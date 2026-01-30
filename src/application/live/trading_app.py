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

from src.domain.trading.state.state_machine import StateMachine, BotState
from src.domain.market.filters import (
    KillConditionsFilter, BlackSwanFilter, HTFFilter,
    LTFTriggerFilter,
    # MTFStructureFilter removed (FIX #1: Enable earlier entries)
    # Advanced filters
    check_funding_rate_safe,
    should_exit_on_funding,
    check_session_quality,
    check_htf_divergence,
    calculate_vpoc,
    check_vpoc_validity,
    check_btc_trend,
    # Specialized filters
    LocationFilter, get_location_filter, LocationContext,
    MicroTrendFilter, get_micro_trend_filter, MicroTrendState,
    EntryFilterPipeline, get_entry_filter_pipeline,
    SpreadFilter, get_spread_filter, check_spread_for_entry, SpreadCheckResult,
)
from src.domain.risk.risk_engine import RiskEngine
from src.infrastructure.exchanges.kraken.execution import round_size_to_exchange, KRAKEN_BTC_MIN_SIZE  # 🔒 FIX: Centralized size rounding + min size
from src.shared.indicators import (
    calculate_ema, calculate_atr, calculate_adx, calculate_rsi, calculate_bollinger_bands
)
from src.config.trading_config import HTFFilters, RiskManagement, OperatingMode  # For logging thresholds + MODE
from src.config.trading_config import DailyRegimeDetector  # 🔥 FIX (Jan 23): Daily regime classification
from src.domain.market.regime import EdgeRegimeClassifier, RegimeDetector, EntryRegime, calculate_chase_atr
from src.domain.market.regime import TrendIntelligence, TrendPhase, get_trend_intelligence  # 🔥 NEW: Trend cycle detection
from src.domain.trading.edge.edge_enforcement import EdgeEnforcement
from src.domain.trading.edge.edge_validator import ContextValidity
# NOTE: PositionSizer class removed - using risk_engine for sizing
from src.shared.time_provider import get_time_provider, TimeProvider
from src.infrastructure.monitoring.error_tracker import get_error_tracker, ErrorSeverity
from src.infrastructure.monitoring.system_health import SystemHealthMonitor
from src.infrastructure.exchanges.kraken.exchange_reconciler import ExchangeReconciler, ReconcilerState
from src.infrastructure.monitoring.opportunity_tracker import MissedOpportunityTracker
from src.domain.market.structure.important_levels import (
    ImportantLevelsProvider, get_important_levels_provider,
    initialize_important_levels, LevelContext, LevelType
)
from src.domain.trading.strategy.pullback_short import (
    PullbackShortEngine, get_pullback_short_engine,
    PullbackShortSignal, TradeType
)
from src.config.trading_config import PullbackShortConfig

# NEW: Bias Strength & Trend Expansion configs (Jan 22, 2026)
from src.config.trading_config import (
    BiasStrength, BiasStrengthConfig, TrendExpansionConfig, ShortObstinacyConfig
)

# NEW: Anticipation & Exhaustion engines (Jan 22, 2026)
from src.domain.trading.anticipation import (
    AnticipationEngine, get_anticipation_engine, AnticipationSignal, AnticipationState
)
from src.domain.trading.strategy.momentum_exhaustion import (
    MomentumExhaustionDetector, get_exhaustion_detector, ExhaustionLevel, ExhaustionState
)

from src.infrastructure.monitoring.metrics import (
    update_pullback_short_signal, update_pullback_short_entry,
    update_pullback_short_exit, update_pullback_short_status
)

# NEW IMPORTS: Slippage, Trade Logger, Dynamic Configs (Jan 16, 2026)
from src.infrastructure.exchanges.kraken.slippage_model import (
    SlippageModel, get_slippage_model, calculate_worst_case_rr, OrderType
)
from src.infrastructure.storage.parquet.trade_logger import TradeLogger, get_trade_logger
from src.config.trading_config import (
    DynamicScoreThreshold, SessionTradeLimits, VolatilityRegime, VolatilityRegimeValue,
    LossStreakConfig, LocationFilterOverride, MicroTrendADXConfig,
    get_session_name
)
from src.domain.trading.scoring import EntryScorer, get_entry_scorer

# Extracted helper modules (Jan 20, 2026 - Phase 4 refactoring)
from src.domain.trading.execution_logic.filter_checks import (
    check_trend_filter as _check_trend_pure,
    check_volatility_expansion as _check_volatility_expansion_pure,
    check_anti_reversal as _check_anti_reversal_pure,
    check_volume_delta as _check_volume_delta_pure,
    check_bos_against_position as _check_bos_pure,
    check_atr_regime_allows_entry as _check_atr_regime_pure,
    check_volatility_minimum as _check_vol_min_pure,
    check_not_post_impulse as _check_post_impulse_pure,
    check_sweep_occurred as _check_sweep_pure,
)
from src.shared.indicator_helpers import (
    calculate_atr_1m as _calc_atr_1m_pure,
    calculate_atr_5m as _calc_atr_5m_pure,
    calculate_atr_generic as _calc_atr_generic_pure,
    find_tp2_target as _find_tp2_pure,
    find_support_resistance as _find_sr_pure,
)

# 🔥 NEW (Jan 22, 2026): Entry Quality Gate - Hard rules before scoring
from src.domain.trading.execution_logic.entry_quality import (
    EntryQualityGate, get_entry_quality_gate, GateDecision, GateResult
)

# 🧠 NEW (Jan 24, 2026): Intelligent Filter - Multi-layer decision system
from src.domain.trading.execution_logic.intelligent_filter import (
    IntelligentFilter, get_intelligent_filter, FilterSeverity
)

# 🔥 NEW (Jan 24, 2026): Adaptive Entry System - Market-aware thresholds
from src.domain.trading.strategy.adaptive_entry import (
    AdaptiveEntrySystem, get_adaptive_entry_system, MarketCondition
)

# 🤖 NEW (Jan 24, 2026): ML Entry Score Refinement - Shadow mode logging
from src.ml.inference import get_ml_inference_engine

# NEW ARCHITECTURE: Position Store as Single Source of Truth
from src.bootstrap.runtime import RuntimeMode, get_runtime_config
from src.infrastructure.storage import get_position_store, TrackedPosition
from src.domain.trading.cost.cost_model import get_cost_model  # 🔥 FIX (Jan 24): Cost validator

# 🔥 NEW (Jan 27, 2026 - AUDIT REFACTOR Point 3): Centralized indicator caching
from src.shared.indicator_cache import get_indicator_cache, IndicatorCache

# 🔥 NEW (Jan 27, 2026 - AUDIT REFACTOR Point 5): Position management utilities
from src.application.live.position_manager import (
    calculate_current_r,
    check_bos_against_position,
    calculate_smart_trailing_stop,
    get_position_health_checker,
    # NEW: Full PositionManager refactor
    PositionManager, get_position_manager,
    ProbingModeChecker, AnticipationChecker, MomentumDecayChecker, BOSInvalidationChecker,
    ExitDecision, ExitCheckResult,
    # Exhaustion tightening
    ExhaustionTighteningChecker
)

# 🔥 NEW (Jan 27, 2026 - AUDIT REFACTOR): Entry execution utilities
from src.application.live.entry_executor import (
    ExecutionGuards, PositionSizeCalculator, LocationGuards, StopLossCalculator
)

# 🔥 NEW (Jan 27, 2026 - AUDIT REFACTOR): Entry validators
from src.application.live.entry_validators import (
    ChaseValidator, get_chase_validator,
    GraduatedSizer, get_graduated_sizer,
    RRValidator, get_rr_validator,
    CostValidator, get_cost_validator,
    MomentumValidator, get_momentum_validator,
    ChaseResult, DefensiveSizingResult, RRValidationResult, CostValidationResult
)

# 🔧 AUDIT REFACTOR (Jan 27, 2026): Exit action handlers
from src.application.live.exit_actions import (
    handle_partial_close,
    handle_add_position,
    handle_full_close
)

# 🔧 AUDIT REFACTOR (Jan 27, 2026): Entry evaluation helpers
from src.application.live.entry_evaluation import (
    check_pre_entry_conditions,
    update_daily_regime,
    PreEntryCheckResult
)

# 🔧 AUDIT REFACTOR (Jan 27, 2026): IDLE state handlers
from src.application.live.idle_state_handlers import (
    check_rejection_cooldown,
    check_candle_gate,
    check_pending_state_conflicts,
    sync_with_exchange_if_needed,
    handle_wait_ltf_state,
    check_anticipation_signal,
    update_daily_regime_if_needed,
    check_pre_trade_conditions,
    refresh_balance_if_needed,
)

# 🔧 AUDIT REFACTOR (Jan 27, 2026): Market analysis helpers
from src.application.live.market_analysis import (
    BiasStrengthDetector, get_bias_strength_detector,
    TrendExpansionDetector, get_trend_expansion_detector,
    ProbingCriteriaChecker, get_probing_checker,
    ShortObstinacyCalculator, get_short_obstinacy_calculator,
    ExpectedRCalculator, get_expected_r_calculator,
    calculate_chase_atr as calculate_chase_atr_helper,
    BiasStrengthResult, TrendExpansionResult, ProbingResult, ExpectedRResult,
)

# 🔧 AUDIT REFACTOR (Jan 27, 2026): Trade recording module
from src.application.live.trade_recording import (
    TradeRecorder, get_trade_recorder, TradeRecordingState
)

# 🔧 AUDIT REFACTOR (Jan 28, 2026): Entry orchestrator - consolidates _execute_entry
from src.application.live.entry_orchestrator import (
    orchestrate_entry, GuardOrchestrator, ScoreOrchestrator, SizingOrchestrator
)

# 🎯 EDGE STATE MANAGER (Jan 27, 2026): Edge como evento discreto
# El sistema SOLO opera cuando hay un edge ACTIVO (BOS + pullback)
# 🔥 Extended (Jan 27, 2026): Soporte para RANGE y BREAKOUT edges
from src.domain.trading.edge_state_manager import (
    EdgeStateManager, get_edge_manager, EdgeState, EdgeType
)

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
    
    def __init__(self, market_data, execution, external_data, trade_logger, output_manager=None, order_book=None, enforcement_mode: str = 'forward'):
        """
        Initialize SignalEngine.
        
        Args:
            market_data: SynchronizedMultiTimeframeBuffer
            execution: Execution engine (real or mock)
            external_data: External data manager
            trade_logger: Trade logger
            output_manager: Output manager for notifications (Telegram)
            order_book: OrderBookManager for spread/depth analysis (optional)
            enforcement_mode: Edge enforcement mode ('backtest', 'forward', 'live')
        """
        self.market_data = market_data
        self.execution = execution
        self.external_data = external_data
        self.trade_logger = trade_logger
        self.output_manager = output_manager
        self.order_book = order_book
        
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
        self.symbol: str | None = None
        
        # Filters - ALL use market_time, backtest_mode flag for relaxed validation
        self.kill_filter = KillConditionsFilter(self.state_machine, capital=0.0, risk_engine=self.risk_engine, backtest_mode=self._is_backtest)
        self.black_swan_filter = BlackSwanFilter()
        self.htf_filter = HTFFilter(backtest_mode=self._is_backtest)
        # self.mtf_filter = MTFStructureFilter()  # REMOVED (FIX #1: Enable earlier entries)
        self.ltf_filter = LTFTriggerFilter(backtest_mode=self._is_backtest)
        
        # Location filter - macro context veto (prevents buying tops in RANGE)
        self.location_filter = get_location_filter()
        self._location_context: LocationContext | None = None

        # =====================================================================
        # MICRO TREND FILTER - Prevents LONGs during micro-bearish trends
        # Detects when 5m structure is bearish even if 1h is bullish
        # =====================================================================
        # 🔥 FIX (Jan 22, 2026): Disable hard_block - only penalize score
        # Hard blocking on micro structure kills too many good trades
        # BTC does LL/LH constantly during trends, doesn't mean reversal
        self.micro_trend_filter = get_micro_trend_filter(hard_block_enabled=False)
        self._micro_trend_state: MicroTrendState | None = None

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
        self._current_trade_regime: str = 'RANGE'  # 🔥 FIX (Jan 22): Track regime for trailing stop
        
        # =====================================================================
        # 🔥 DAILY REGIME DETECTOR (Jan 23, 2026) - IMPRESCINDIBLE
        # Classifies day as TREND_DAY, RANGE_DAY, or TRANSITION_DAY at session start
        # This OVERRIDES intraday regime decisions for the entire day
        # =====================================================================
        self._daily_regime: str = 'RANGE_DAY'  # Default conservative
        self._daily_regime_config: dict = DailyRegimeDetector.get_daily_config('RANGE_DAY')
        self._daily_regime_last_update: datetime | None = None
        
        # =====================================================================
        # 🔥 TREND INTELLIGENCE (Jan 23, 2026) - Detección de ciclos
        # Detecta INICIO y FIN de tendencias, no solo estado actual
        # Fases: ACCUMULATION → BIRTH → ACCELERATION → MATURE → EXHAUSTION → DEATH
        # =====================================================================
        self._trend_intelligence = get_trend_intelligence()
        self._trend_phase: TrendPhase = TrendPhase.RANGE_BOUND
        self._trend_last_update: datetime | None = None
        
        # Regime classifier for volume-aware filtering
        self.regime_classifier = EdgeRegimeClassifier()
        
        # Edge enforcement - pre-trade validation & degradation monitoring
        # Uses Activity Regime model (not session-based) for 24/7 crypto markets
        self.edge_enforcement = EdgeEnforcement(mode=self._enforcement_mode)
        
        # =====================================================================
        # ENTRY SCORER - Extracted to reduce god class complexity (Jan 20, 2026)
        # Handles score calculation: structure, timing, momentum, context
        # =====================================================================
        self._entry_scorer = get_entry_scorer()
        
        # =====================================================================
        # ENTRY QUALITY GATE - Hard rules before scoring (Jan 22, 2026)
        # Layer 1: Gate (hard rules) → Layer 2: Score → Layer 3: Execution
        # =====================================================================
        self._entry_gate = get_entry_quality_gate()
        
        # =====================================================================
        # 🧠 INTELLIGENT FILTER - Multi-layer decision system (Jan 24, 2026)
        # Accumulates penalties, no "entering anyway" - blocks if accumulated risk is too high
        # =====================================================================
        self._intelligent_filter = get_intelligent_filter()
        
        # =====================================================================
        # 🔥 ADAPTIVE ENTRY SYSTEM (Jan 24, 2026)
        # Market-aware thresholds that adjust based on:
        # - Volatility (BB width, ADX)
        # - Activity level (hours since last trade)
        # - Market condition (dead, low, normal, high, extreme)
        # =====================================================================
        self._adaptive_entry = get_adaptive_entry_system()
        
        # =====================================================================
        # 🤖 ML INFERENCE ENGINE (Jan 24, 2026) - Entry Score Refinement
        # SHADOW MODE: Logs predictions without affecting decisions
        # Future: HYBRID mode will adjust score ±1 based on ML confidence
        # =====================================================================
        self._ml_inference = get_ml_inference_engine()
        
        # 🤖 ML STATUS LOG - Show ML state at startup
        ml_stats = self._ml_inference.get_stats()
        logger.info(f"🤖 ML ENGINE STATUS:")
        logger.info(f"   Phase: {ml_stats['phase']}")
        if ml_stats['model_loaded']:
            logger.info(f"   Model: LOADED ✅ (AUC: {ml_stats['model_auc']:.3f})")
            logger.info(f"   Drift Detection: {'ACTIVE ✅' if not ml_stats['drift_detected'] else '⚠️ DRIFT DETECTED'}")
        else:
            logger.info(f"   Model: NOT YET TRAINED")
            logger.info(f"   → ML is collecting data in SHADOW mode")
            logger.info(f"   → Run training after 100+ trades: uv run python -m src.ml.training.train_entry_model")
        
        # NOTE: Position sizing is handled by RiskEngine (not separate PositionSizer)
        
        # =====================================================================
        # 🎯 EDGE STATE MANAGER (Jan 27, 2026) - Edge como EVENTO DISCRETO
        # 
        # Filosofía: El sistema NO busca entradas continuamente.
        # Solo opera cuando hay un EDGE ACTIVO.
        # 
        # Estados:
        #   NO_EDGE: Sin oportunidad - NO SE EVALUA NADA
        #   EDGE_ARMED: BOS detectado, esperando pullback
        #   EDGE_ACTIVE: Pullback válido, ventana de entrada abierta
        #   EDGE_EXPIRED: Oportunidad consumida o expirada
        # 
        # Reglas:
        #   - Máximo 2 trades por edge event
        #   - Edge expira después de X minutos sin ejecución
        #   - Si ambos trades fallan, edge está "quemado"
        # =====================================================================
        self._edge_manager = get_edge_manager()
        self._last_swing_high: float = 0.0
        self._last_swing_low: float = 0.0
        self._prev_swing_high: float = 0.0
        self._prev_swing_low: float = 0.0
        
        # Edge context (updated periodically)
        self._edge_context: ContextValidity | None = None
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
        self.current_bias: str | None = None  # 'LONG' or 'SHORT'
        self.impulse_high: float = 0.0
        self.impulse_low: float = 0.0
        self.pending_order_id: str | None = None
        self.entry_order_time: datetime | None = None
        
        # STATE MACHINE para evitar busy loops
        # 🔥 FIX: Type annotation to allow str values
        self.setup_state: str | None = None  # IDLE, WAIT_LTF, EXECUTING
        self.setup_start_time: datetime | None = None  # Cuándo entró en WAIT_LTF
        self.ltf_wait_timeout = 5 * 60  # 5 minutos (300s) = ~5 candles 1m
        self.setup_frozen_htf = False  # Freeze HTF evaluation
        # self.setup_frozen_mtf = False  # REMOVED (no MTF filter)
        self.last_setup_discard_time: datetime | None = None  # Cuándo se descartó el último setup
        self.setup_discard_cooldown = 10  # Cooldown reducido: solo 10s para evitar thrashing
        # Position management flags
        self._stops_set = False  # Flag para evitar intentar establecer SL/TP múltiples veces
        self._sync_lock = asyncio.Lock()  # 🔥 AUDIT FIX: Proper async lock for sync operations
        self._syncing_position = False  # Track if sync is in progress
        # 🔥 FIX (Jan 23, 2026): Force BOS check after orphan recovery
        self._force_bos_check_on_orphan = False  # Check BOS immediately after orphan sync
        
        # =====================================================================
        # MISSING ATTRIBUTE INITIALIZATION (🔥 AUDIT FIX)
        # These were previously initialized via hasattr() checks - now explicit
        # =====================================================================
        self._last_entry_data = None  # Entry data for current trade
        # 🔥 FIX: Add type annotations for datetime | None attributes
        self._last_exchange_sync: datetime | None = None  # Last time we synced with exchange
        self._consecutive_post_only_rejects = 0  # Track post-only rejections
        self._recent_trade_prices = []  # Recent trade prices for analysis
        self._last_log_time: datetime | None = None  # Rate limiting for logs
        self._last_capital_warning: datetime | None = None  # Rate limiting for capital warnings
        self._last_exchange_state_check: datetime | None = None  # Last exchange state check
        self._last_exchange_unavailable_log: datetime | None = None  # Rate limit exchange unavailable logs
        self._consecutive_no_position_checks = 0  # Track consecutive no-position checks
        self._last_reconcile_time: datetime | None = None  # Last position reconciliation time
        self._last_incomplete_log: datetime | None = None  # Rate limit incomplete data logs
        self._last_defensive_log: datetime | None = None  # Rate limit defensive logs
        self._last_balance_check: datetime | None = None  # Last balance check time
        self._last_htf_reject_log_time: datetime | None = None  # Rate limit HTF reject logs
        self._last_location_state = None  # Last location filter state
        self._last_micro_state = None  # Last micro trend state
        self._last_fp_block: str | None = None  # Last filter pipeline block (blocker_bias string)
        self._last_pullback_block_log: datetime | None = None  # Rate limit pullback block logs
        self._loss_exit_types = {}  # Track loss exit types for analysis (dict not list!)
        
        # =====================================================================
        # 💰 SESSION TRACKING (Jan 28, 2026)
        # Track session start capital for PnL reporting
        # =====================================================================
        self._session_start_capital: float | None = None  # Capital at session start
        self._session_start_time: datetime | None = None  # Session start time
        
        # =====================================================================
        # 🔥 ANOMALY DETECTION TRACKING (Jan 24, 2026)
        # Track previous tick data to detect anomalies (gaps, staleness)
        # =====================================================================
        self._prev_tick_price: float = 0.0  # Previous tick price for gap detection
        self._prev_tick_time: datetime | None = None  # Previous tick time for staleness detection
        
        # =====================================================================
        # INDICATOR CACHE - Centralized caching to avoid 7+ recalculations
        # 🔥 REFACTORED (Jan 27, 2026 - AUDIT Point 3): Now uses IndicatorCache
        # =====================================================================
        self._indicator_cache_v2 = get_indicator_cache()  # Centralized cache
        
        # =====================================================================
        # LOSS STREAK COOLDOWN - Protection against chop regimes
        # 
        # FIX (Jan 22, 2026): Punto 3 - Loss cooldown was too aggressive
        # PROBLEM: 2 losses triggered 30min cooldown - this is normal variance
        # SOLUTION: Tiered cooldown with higher thresholds:
        #   - 3 consecutive losses → 15 min cooldown (tier 1)
        #   - 4 consecutive losses → 60 min cooldown (tier 2)
        #   - 5 consecutive losses → 180 min cooldown (tier 3)
        #   - 6 consecutive losses → KILL SWITCH (daily)
        # 
        # 🔥 FIX (Jan 25, 2026): Enhanced auto kill-switch + dead hours tracking
        # =====================================================================
        self._consecutive_losses = 0  # Tracks consecutive SL hits
        self._cooldown_until = None  # Timestamp when cooldown ends
        self._kill_switch_active = False  # Full daily kill switch
        self._daily_losses_count = 0  # Count of losses today
        self._dead_hours_losses = 0   # Losses during dead hours (4am-7am UTC)
        self._dead_hours_blocked = False  # Block trading in dead hours after 2+ losses
        
        # Cooldown configuration from config
        from src.config.trading_config import OperatingMode
        self._pause_after_3 = getattr(OperatingMode, 'PAUSE_AFTER_LOSSES_3', 15)
        self._pause_after_4 = getattr(OperatingMode, 'PAUSE_AFTER_LOSSES_4', 60)
        self._pause_after_5 = getattr(OperatingMode, 'PAUSE_AFTER_LOSSES_5', 180)
        self._kill_switch_losses = getattr(OperatingMode, 'KILL_SWITCH_LOSSES', 6)
        
        # =====================================================================
        # REJECTION COOLDOWN - Prevents rapid reevaluation after filter blocks
        # Only activated for TECHNICAL errors, not filter rejections
        # =====================================================================
        self._rejection_cooldown_until: datetime | None = None  # When rejection cooldown ends
        self._rejection_cooldown_candles = 2  # Number of candles to wait after rejection
        self._last_rejection_reason: str | None = None  # Why was the setup rejected
        self._last_evaluated_candle_ts: int = 0  # Timestamp of last evaluated LTF candle
        
        # =====================================================================
        # EVENT-BASED EVALUATION GATE (Jan 28, 2026)
        # - Ticks since last evaluation (for max ticks fallback)
        # - Last evaluated price (for price movement detection)
        # =====================================================================
        self._ticks_since_last_eval: int = 0  # Ticks since last full evaluation
        self._last_eval_price: float = 0.0    # Price at last evaluation
        
        # =====================================================================
        # MOMENTUM QUALITY - For position sizing, not hard veto
        # =====================================================================
        self._last_momentum_quality: str = "MODERATE"  # STRONG, MODERATE, WEAK
        self._last_momentum_atr: float = 100.0  # Last ATR value
        self._last_spread_bps: float = 1.0  # Last spread in basis points (for SL updates)
        
        # =====================================================================
        # ENTRY SCORE SYSTEM - Score > Gates architecture
        # 4 blocks × 0-2 points + synergy bonus = max 9
        # Uses MODE from OperatingMode for thresholds
        # Only Structure=0 blocks entry (the only real gate)
        # =====================================================================
        self._entry_score: int | float = 0
        self._score_breakdown: dict = {
            'structure': 0,  # HTF alignment + 5m structure (0-2)
            'timing': 0,     # BOS at close + reclaim quality (0-2)
            'momentum': 0,   # ADX + slope (0-2)
            'context': 0,    # Session + volatility (0-2)
            'synergy': 0     # HTF clean + BOS confirmed = +1 (non-linear bonus)
        }
        # Threshold from OperatingMode - NEVER change based on losses
        self._entry_threshold: float = float(OperatingMode.get_score_threshold())
        
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
        self._last_bos_level: float | None = None  # Nivel donde se detectó BOS
        self._last_bos_direction: str | None = None  # 'LONG' o 'SHORT'
        
        # =====================================================================
        # HTF CACHE - Solo recalcular cuando cierra nueva vela 1H
        # =====================================================================
        self._htf_cache_ts: int = 0  # Timestamp de la última vela 1h evaluada
        self._htf_cache_passed: bool = False  # Resultado cacheado
        self._htf_cache_bias: str | None = None  # 'LONG', 'SHORT', None
        self._htf_cache_reason: str = ""  # Razón del resultado
        # 🔥 FIX (Jan 23, 2026): Cache HTF regime mode based on ADX
        self._htf_cache_regime_mode: str = 'UNKNOWN'  # 'RANGE', 'WEAK_TREND', 'TREND', 'LOW_ADX'
        self._htf_cache_adx: float = 0.0  # ADX value for logging
        
        # =====================================================================
        # 🔥 BIAS ARCHITECTURE FIX (Jan 28, 2026) - Separar conceptos
        #
        # STRUCTURAL_BIAS: Derivado de HTF (1h, daily)
        #   - Sirve para sizing y evitar contra-tendencia fuerte
        #   - NO bloquea trades
        #   - Valores: 'LONG' / 'SHORT' / None
        #
        # ACTIVE_DIRECTION: Derivado de momentum/displacement
        #   - Sirve para decidir qué buscar AHORA
        #   - Valores: 'LONG' / 'SHORT' (NUNCA None/NEUTRAL)
        #   - Si no hay edge → NO_EDGE pero no NEUTRAL
        #
        # FORBIDDEN_DIRECTION: Dirección bloqueada por invalidación
        #   - Si LONG invalidado → forbidden='LONG', pero SHORT aún permitido
        # =====================================================================
        self._structural_bias: str | None = None  # HTF bias (doesn't block trades)
        self._active_direction: str | None = None  # Current momentum direction
        self._forbidden_direction: str | None = None  # Direction blocked by invalidation
        self._displacement_bypass: bool = False  # Bypass filters due to strong displacement
        self._strong_displacement: bool = False  # Strong displacement overrides BIAS FLIP
        
        # =====================================================================
        # 🔥 EXPANSION MODE (Jan 29, 2026) - Estado explícito para mercado en expansión
        #
        # Cuando el mercado está en EXPANSION:
        # - forbidden_direction SUSPENDIDO (no ignorado permanentemente)
        # - BIAS FLIP threshold ALTO (3x normal)
        # - Solo se invalida por: displacement opuesto, estructura HTF, o timeout
        #
        # Esto evita que pequeños rebotes invaliden trades durante tendencias fuertes
        # =====================================================================
        self._expansion_mode: bool = False  # Estado de mercado en expansión
        self._expansion_direction: str | None = None  # Dirección del expansion (LONG/SHORT)
        self._expansion_start_time: datetime | None = None  # Cuando empezó el expansion
        self._expansion_initial_displacement: float = 0.0  # Magnitud inicial del displacement
        # 🌊 BE WATER: Timeout ADAPTATIVO basado en volatilidad
        # Base 45 min, pero escala: alta vol → timeout corto, baja vol → timeout largo
        self._expansion_timeout_base: int = 45  # Base timeout
        self._expansion_timeout_minutes: int = 45  # Actual (recalculado dinámicamente)
        self._current_volatility_ratio: float = 1.0  # ATR_actual / ATR_promedio
        
        # =====================================================================
        # 🔥 DIRECTION LOCK (Jan 29, 2026) - Lock direccional post-DISPLACEMENT
        # 
        # Separación OBLIGATORIA: MarketBias ≠ TradePermission
        # - MarketBias: puede cambiar libremente (SHORT/LONG/NEUTRAL)
        # - TradePermission: solo cambia con reglas duras (ALLOW/BLOCK)
        # - DirectionLock: bloquea cambios de dirección tras displacement
        #
        # Durante LOCK:
        # ❌ No bias flip
        # ❌ No forbid por retrocesos pequeños
        # ✅ Solo invalidación MAYOR (3x displacement inicial)
        # =====================================================================
        self._direction_lock: str | None = None  # LONG/SHORT/None
        self._direction_lock_reason: str | None = None  # "DISPLACEMENT"/"STRONG_MOVE"/etc
        self._direction_lock_expires_at: datetime | None = None  # Cuando expira el lock
        self._direction_lock_initial_move: float = 0.0  # Movimiento que causó el lock
        # 🌊 BE WATER: Lock timeout ADAPTATIVO
        self._direction_lock_timeout_base: int = 30  # Base timeout
        self._direction_lock_timeout_minutes: int = 30  # Actual (recalculado)
        
        # 🌊 BE WATER: Invalidación asimétrica ADAPTATIVA por régimen
        # RANGE: 2.0x (más fácil invalidar - micro moves importan)
        # TREND: 3.5x (más difícil invalidar - confiar en momentum)
        # EXPANSION: 4.0x (muy difícil invalidar - dejar correr)
        self._asymmetric_invalidation_base: float = 3.0  # Base
        self._asymmetric_invalidation_mult: float = 3.0  # Actual (recalculado)
        
        # 🌊 BE WATER: Last known volatility ratio for adaptive calculations
        self._last_vol_ratio: float = 1.0  # atr_5m / atr_1m
        self._last_trading_mode: str = 'TREND'  # RANGE/TREND/TRANSITION
        
        # Price tracking for invalidation
        self._bias_established_price: float = 0.0  # Price when bias was set
        self._bias_established_time: datetime | None = None  # When bias was set
        self._bias_high_since: float = 0.0  # Highest price since bias established
        self._bias_low_since: float = 0.0  # Lowest price since bias established
        
        # BIAS RESTORATION tracking (reversible invalidation)
        self._bias_invalidation_level: float = 0.0  # Level to reclaim for restoration
        self._bias_before_invalidation: str | None = None  # What bias was before invalidation
        self._cached_micro_momentum: str | None = None  # Cached micro momentum result
        
        # =====================================================================
        # SYSTEM HEALTH MONITOR - Independent of PnL
        # =====================================================================
        self.health_monitor: SystemHealthMonitor | None = None  # Initialized in initialize()
        
        # =====================================================================
        # MISSED OPPORTUNITY TRACKER - Track blocked signals for edge analysis
        # =====================================================================
        self.missed_tracker = MissedOpportunityTracker()
        
        # =====================================================================
        # REGIME DETECTOR - Context-aware market state (Jan 12, 2026)
        # Anti-chasing becomes relative to regime, not absolute
        # =====================================================================
        self.regime_detector = RegimeDetector()
        self._current_regime: EntryRegime = EntryRegime.NORMAL  # Default: standard trading
        
        # =====================================================================
        # IMPORTANT LEVELS PROVIDER - Anchor levels for S/R (Jan 13, 2026)
        # Year/Quarter/Month/Week closes act as psychological S/R
        # Used for: blocking near levels, confluence bonus, TP adjustment
        # =====================================================================
        self.important_levels: ImportantLevelsProvider | None = None  # Initialized async in initialize()
        self._last_level_context: LevelContext | None = None  # Last computed level context
        self._last_levels_update: datetime | None = None  # Last time levels were refreshed
        
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
        self._current_trade_id: str | None = None  # Active trade ID for logging
        
        # =====================================================================
        # NEW: SESSION TRADE COUNTER - Limit trades per session (Jan 16, 2026)
        # =====================================================================
        self._session_trade_counts: dict = {}  # {session_name: count}
        self._last_session: str | None = None
        self._daily_trade_count: int = 0
        self._last_trade_date: str | None = None
        self._at_session_limit: bool = False  # Track if at session limit
        
        # =====================================================================
        # NEW: VOLATILITY REGIME TRACKING (Jan 16, 2026)
        # =====================================================================
        self._current_volatility_regime: VolatilityRegimeValue | str = VolatilityRegime.NORMAL  # Uses VolatilityRegime class constants
        self._last_atr_ratio: float = 1.0
        
        # =====================================================================
        # NEW: LAST SENT SL TRACKING for exchange updates (Jan 16, 2026)
        # =====================================================================
        self._last_sent_sl: float = 0.0
        
        # =====================================================================
        # NEW: BIAS STRENGTH & TREND EXPANSION SYSTEM (Jan 22, 2026)
        # =====================================================================
        self._current_bias_strength: 'BiasStrength | None' = None  # BULL_STRONG, BEAR_STRONG, NEUTRAL
        self._trend_expansion_active: bool = False
        self._trend_expansion_start_time: datetime | None = None
        self._anti_fade_until: datetime | None = None  # No counter-trend until this time
        self._countertrend_losses_session: int = 0  # CT losses this session
        self._last_countertrend_loss_time: datetime | None = None
        self._short_cooldown_until: datetime | None = None  # Short obstinacy cooldown
        self._last_expansion_check_ts: int = 0  # Cache for expansion check
        
        # =========================================================================
        # 🔥 NEW (Jan 26, 2026): FALSE EXPANSION TRACKING
        # 
        # A "false expansion" is when:
        # - Expansion was detected (4/4 criteria)
        # - Trade entered
        # - MFE < 0.3R AND stop hit
        # 
        # This tracks detector quality, NOT entry quality.
        # High false expansion rate = detector needs recalibration.
        # =========================================================================
        self._expansion_trades_count: int = 0  # Total trades in expansion mode
        self._false_expansion_count: int = 0   # Trades where expansion failed (MFE<0.3R + SL hit)
        self._consecutive_expansion_losses: int = 0  # For cluster protection
        
        # =====================================================================
        # NEW: ANTICIPATION ENGINE - Early entries BEFORE breakouts (Jan 22, 2026)
        # Detects: BB squeeze, ADX coiling, volume decline, structure building
        # =====================================================================
        self.anticipation_engine = get_anticipation_engine()
        self._last_anticipation_state: AnticipationState | None = None
        
        # =====================================================================
        # NEW: EXHAUSTION DETECTOR - Exit at the "summum" (Jan 22, 2026)
        # Detects: Volume climax, RSI divergence, candle exhaustion, ATR reversal
        # =====================================================================
        self.exhaustion_detector = get_exhaustion_detector()
        self._last_exhaustion_state: ExhaustionState | None = None
        
        # =====================================================================
        # 🔧 AUDIT REFACTOR (Jan 27, 2026): Position Manager
        # Orchestrates all position exit checks (probing, anticipation, BOS, momentum decay)
        # =====================================================================
        self._position_manager = get_position_manager()
        
        # =====================================================================
        # 🔧 AUDIT REFACTOR (Jan 27, 2026): Entry Validators
        # Extracted from _execute_entry: chase, sizing, RR, cost validation
        # =====================================================================
        self._chase_validator = get_chase_validator(self.regime_detector, self._entry_threshold)
        self._graduated_sizer = get_graduated_sizer()
        self._rr_validator = get_rr_validator(self.slippage_model)
        self._cost_validator = get_cost_validator()
        self._momentum_validator = get_momentum_validator()
        
        # =====================================================================
        # 🔧 AUDIT REFACTOR (Jan 27, 2026): Market Analysis Helpers
        # Extracted: bias strength, trend expansion, probing, short obstinacy, expected R
        # =====================================================================
        self._bias_strength_detector = get_bias_strength_detector()
        self._trend_expansion_detector = get_trend_expansion_detector()
        self._probing_checker = get_probing_checker()
        self._short_obstinacy_calculator = get_short_obstinacy_calculator()
        self._expected_r_calculator = get_expected_r_calculator()
        
        # 🔧 AUDIT REFACTOR (Jan 27, 2026): Trade recording module
        self._trade_recorder = get_trade_recorder()
        
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
        if self._last_exchange_state_check is None:
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
                if self._last_exchange_unavailable_log is None or \
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
                    
                    # 🔥 FIX (Jan 23, 2026): Force BOS evaluation on next cycle for orphan recovery
                    # An orphan position may already be invalidated by structure break
                    self._force_bos_check_on_orphan = True
                    logger.info("📊 Orphan recovered - will check BOS invalidation on next cycle")
                
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
                        if self.symbol is None:
                            logger.error("Cannot check position_store: symbol is None")
                        else:
                            ps_pos = self._position_store.get_position(self.symbol)
                            if ps_pos and not ps_pos.is_flat and ps_pos.size > MIN_POSITION_SIZE:
                                # This is weird - sync should have cleared it. Log but proceed anyway
                                logger.error(f"🚨 position_store still has {ps_pos.side} {ps_pos.size:.6f} after sync - forcing clear")
                                ps_pos.size = 0
                                ps_pos.side = 'FLAT'
                    except Exception as ps_e:
                        logger.debug(f"Position store sync failed: {ps_e}")
                    
                    logger.critical(f"🔄 STATE OVERRIDE: MANAGE but exchange has no position (confirmed 3x) → forcing IDLE")
                    
                    # Clear local position (thread-safe)
                    if self.risk_engine.has_open_position():
                        logger.warning("⚠️ Clearing orphan local position")
                        await self.risk_engine.clear_position("orphan local position - exchange has none")
                    
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
        from src.infrastructure.exchanges.kraken.exchange_reconciler import PublicState
        
        # 🔒 CRITICAL FIX (Jan 21, 2026): Don't interfere if we're in HARD_LOCK
        # HARD_LOCK means we're actively executing an operation and will handle state ourselves
        if self.reconciler.is_hard_locked:
            logger.debug(f"Reconciler callback skipped: HARD_LOCK active (state={public_state})")
            return
        
        # If reconciler says IDLE and we're in MANAGE, the position was closed externally
        if public_state == PublicState.IDLE and self.state_machine.state == BotState.MANAGE:
            logger.critical("📤 RECONCILER DETECTED: Position closed on exchange (TP/SL hit)")
            
            # Record trade close with estimated PnL
            if self.risk_engine.has_open_position():
                position = self.risk_engine.position
                if position is None:  # Type narrowing for Pyright
                    return
                
                # =========================================================================
                # 🔥 FIX (Jan 27, 2026): Estimate fill price from known TP/SL levels
                # This is a SYNC callback so we can't call async exchange methods.
                # Use the TP or SL price that was set on the position as the estimate.
                # =========================================================================
                exit_price = None
                
                # Get the last candle price as reference
                candles = self.market_data.get_candles('1m')
                current_price = candles[-1]['close'] if candles and len(candles) > 0 else position.entry_price
                
                # Determine if it was TP or SL hit based on current price vs entry
                if position.direction == 'SHORT':
                    # SHORT: TP is below entry, SL is above entry
                    if current_price < position.entry_price:
                        # Price is below entry = likely TP hit
                        exit_price = position.take_profit if hasattr(position, 'take_profit') and position.take_profit else current_price
                    else:
                        # Price is above entry = likely SL hit
                        exit_price = position.stop_loss if hasattr(position, 'stop_loss') else current_price
                else:  # LONG
                    # LONG: TP is above entry, SL is below entry
                    if current_price > position.entry_price:
                        # Price is above entry = likely TP hit
                        exit_price = position.take_profit if hasattr(position, 'take_profit') and position.take_profit else current_price
                    else:
                        # Price is below entry = likely SL hit
                        exit_price = position.stop_loss if hasattr(position, 'stop_loss') else current_price
                
                # Fallback to current price if we still don't have an exit price
                if exit_price is None or exit_price <= 0:
                    exit_price = current_price
                    logger.warning(f"⚠️ Using current price as exit estimate: {exit_price:.2f}")
                else:
                    logger.info(f"📊 Using TP/SL level as exit price: {exit_price:.2f}")
                
                # =========================================================================
                # 🔥 FIX (Jan 27, 2026): Use CORRECT position size for PnL calculation
                # After partials/ADD TO WINNER, position.size tracks remaining size
                # =========================================================================
                actual_closed_size = position.size  # Use current tracked size
                
                # Calculate PnL with correct price and size
                if position.direction == 'LONG':
                    pnl_estimate = (exit_price - position.entry_price) * actual_closed_size
                else:
                    pnl_estimate = (position.entry_price - exit_price) * actual_closed_size
                
                # Calculate R realized for notification
                initial_risk = abs(position.entry_price - position.initial_stop_loss)
                r_realized = pnl_estimate / (initial_risk * actual_closed_size) if initial_risk > 0 and actual_closed_size > 0 else 0
                
                # Determine exit reason (TP or SL)
                if position.direction == 'LONG':
                    exit_type = 'EXCHANGE_TAKE_PROFIT' if exit_price > position.entry_price else 'EXCHANGE_STOP_LOSS'
                else:
                    exit_type = 'EXCHANGE_TAKE_PROFIT' if exit_price < position.entry_price else 'EXCHANGE_STOP_LOSS'
                
                logger.info(f"✅ Trade closed by exchange TP/SL | Entry: {position.entry_price:.2f} → Exit: {exit_price:.2f} | Size: {actual_closed_size:.6f} | PnL: ${pnl_estimate:.2f} ({r_realized:.2f}R)")
                
                # Log trade close - 🔥 FIX: Use proper exit_type so losses are counted correctly
                self.state_machine.record_trade_close(pnl_estimate, pnl_estimate > 0, exit_reason=exit_type)
                self.state_machine.update_daily_pnl(pnl_estimate)
                
                # ═══════════════════════════════════════════════════════════════
                # 🔥 FIX (Jan 25, 2026): Record trade result for KILL-SWITCH tracking
                # Exchange TP/SL closes MUST be counted for consecutive loss tracking
                # Without this, kill-switch and tiered cooldowns don't trigger!
                # ═══════════════════════════════════════════════════════════════
                from datetime import datetime, UTC
                market_time = datetime.now(UTC)
                self._record_trade_result(exit_type, pnl_estimate, market_time)
                logger.info(f"📊 Exchange TP/SL result recorded for loss tracking: {exit_type} ${pnl_estimate:.2f}")
                
                # =========================================================================
                # 🔥 FIX (Jan 27, 2026): LOG TRADE TO TRADE_LOGS FOR EXCHANGE TP/SL CLOSES
                # Previously, trades closed by exchange were NOT logged, causing PnL mismatches
                # =========================================================================
                try:
                    # 🔥 FIX: Use calculate_mfe_r() method instead of non-existent attribute
                    mfe_r = position.calculate_mfe_r() if hasattr(position, 'calculate_mfe_r') else 0
                    mae_r = abs(r_realized) if r_realized < 0 else 0
                    
                    async def _log_exchange_close():
                        await self.trade_logger.log_event('trade_closed', {
                            'entry': position.entry_price,
                            'exit': exit_price,
                            'size': actual_closed_size,
                            'pnl': pnl_estimate,
                            'reason': exit_type,
                            'r': r_realized,
                            'mfe_r': mfe_r,
                            'mae_r': mae_r,
                            'direction': position.direction,
                            'regime': self._current_regime.value if self._current_regime else 'UNKNOWN'
                        })
                    
                    _create_safe_task(_log_exchange_close(), "log_exchange_tpsl_close")
                    logger.info(f"📝 Trade logged: {exit_type} @ {exit_price:.2f} | PnL: ${pnl_estimate:.2f}")
                except Exception as log_err:
                    logger.error(f"Failed to log exchange TP/SL trade: {log_err}")
                
                # 🔥 FIX (Jan 14, 2026): Send Telegram notification for exchange TP/SL closes
                # 🔥 FIX (Jan 27, 2026): Include initial_stop and size for fee calculation
                if self.output_manager is not None:
                    try:
                        _create_safe_task(self.output_manager.send_exit_notification({
                            'symbol': self.symbol,
                            'reason': exit_type,  # Already has EXCHANGE_ prefix
                            'pnl': pnl_estimate,
                            'r_realized': r_realized,
                            'exit_price': exit_price,  # 🔥 FIX: Use actual fill price, not current market
                            'entry_price': position.entry_price,
                            'direction': position.direction,
                            'size': actual_closed_size,  # 🔥 FIX: For fee calculation
                            'initial_stop': position.initial_stop_loss,  # 🔥 FIX: For R net calculation
                            'timestamp': market_time.strftime('%Y-%m-%d %H:%M:%S UTC')
                        }), "send_exit_notification_exchange_tpsl")
                        logger.info("📱 Telegram notification sent for exchange TP/SL close")
                    except Exception as e:
                        logger.error(f"Failed to send Telegram notification for TP/SL: {e}")
            
            # Clear local state (sync - we're in a sync callback)
            self.risk_engine.clear_position_sync("Exchange TP/SL hit - reconciler detected")
            self._reset_trade_state("Exchange TP/SL hit - reconciler detected")
            self.state_machine.transition_to(BotState.IDLE, "Exchange TP/SL filled")
            self._consecutive_no_position_checks = 0
            
            # Also clear position_store
            try:
                if self.symbol is not None:
                    ps_pos = self._position_store.get_position(self.symbol)
                    if ps_pos:
                        ps_pos.size = 0
                        ps_pos.side = 'FLAT'
            except Exception as ps_err:
                logger.debug(f"Position store cleanup skipped: {ps_err}")
    
    @property
    def _symbol(self) -> str:
        """Return symbol as non-None str, raising if not set.
        
        Use this property in places where Pyright complains about
        'str | None' not being assignable to 'str'. This property
        guarantees the return type is str (or raises RuntimeError).
        """
        if self.symbol is None:
            raise RuntimeError("SignalEngine.symbol not initialized")
        return self.symbol

    def _reset_trade_state(self, reason: str = "State reset"):
        """Limpia COMPLETAMENTE el estado de trading - CRITICAL para prevenir órdenes múltiples"""
        logger.info(f"🔄 Resetting trade state: {reason}")
        
        # 🔥 FIX (Jan 20, 2026): Clear position_store on state reset
        # This ensures position tracking is always in sync after exits
        try:
            if hasattr(self, '_position_store') and self._position_store and self.symbol is not None:
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
        
        # 🔥 FIX (Jan 22, 2026): Reset exhaustion detector on trade close
        # Prevents stale peak_atr/peak_velocity from affecting next trade
        if hasattr(self, 'exhaustion_detector') and self.exhaustion_detector:
            self.exhaustion_detector.reset()
        
        # Reset exhaustion tightening flag
        self._exhaustion_tightened = False
        
        # 🔥 FIX (Jan 29, 2026 - AUDIT BUG 4): Reset EXPANSION_MODE and DIRECTION_LOCK
        # Prevents state leakage between trades
        self._expansion_mode = False
        self._expansion_direction = None
        self._expansion_start_time = None
        self._expansion_initial_displacement = 0.0
        self._direction_lock = None
        self._direction_lock_reason = None
        self._direction_lock_expires_at = None
        self._direction_lock_initial_move = 0.0
        self._displacement_bypass = False
        self._strong_displacement = False
        
    def _init_health_monitor(self):
        """Initialize health monitor (called after risk engine capital is set)"""
        if not hasattr(self, 'health_monitor') or self.health_monitor is None:
            self.health_monitor = SystemHealthMonitor()
            logger.info("SystemHealthMonitor initialized")
    
    def _set_rejection_cooldown(self, reason: str, candles: int | None = None):
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
        cooldown_end = market_time + timedelta(seconds=cooldown_seconds)
        self._rejection_cooldown_until = cooldown_end
        self._last_rejection_reason = reason
        
        # Log only once (not on every tick)
        logger.info(f"⏸️ Rejection cooldown: {reason} | Wait {candles} candles until {cooldown_end.strftime('%H:%M:%S')}")
    
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
            # 🔥 FIX (Jan 20, 2026): Add timeout to prevent hanging
            try:
                real_orders = await asyncio.wait_for(
                    self.execution.get_open_orders(self.symbol),
                    timeout=10.0
                )
            except asyncio.TimeoutError:
                logger.error("⏰ TIMEOUT in _reconcile_orders - skipping reconciliation")
                return
            
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
            # 🔒 FIX (Jan 22, 2026): Also sync equity values from real exchange balance
            # Without this, equity stays at default 10k causing incorrect DD calculations
            if self.risk_engine.initial_equity == 10000.0 or self.risk_engine.equity == 10000.0:
                logger.info(f"💰 LIVE: Syncing equity from exchange (was default 10k)")
                self.risk_engine.initial_equity = capital
                self.risk_engine.equity = capital
                self.risk_engine.max_equity = capital
            
            # 💰 NEW (Jan 28, 2026): Track session start capital for PnL reporting
            if self._session_start_capital is None:
                self._session_start_capital = capital
                self._session_start_time = datetime.now(UTC)
                logger.info(f"📊 SESSION START: Capital=${capital:.2f}")
            
            logger.info(f"✅ Capital loaded via get_available_capital: ${self.risk_engine.capital:.2f}")
            logger.info(f"✅ Equity synced: initial=${self.risk_engine.initial_equity:.2f}, current=${self.risk_engine.equity:.2f}")
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
        
        # =====================================================================
        # 🔥 TREND INTELLIGENCE WARM START (Jan 24, 2026)
        # Initialize with historical 1h data for accurate phase/confidence
        # =====================================================================
        try:
            candles_1h = self.market_data.get_candles('1h')
            if candles_1h and len(candles_1h) >= 20:
                logger.info(f"🔥 Warming up TrendIntelligence with {len(candles_1h)} 1h candles...")
                
                highs = [float(c.get('high', 0)) for c in candles_1h]
                lows = [float(c.get('low', 0)) for c in candles_1h]
                closes = [float(c.get('close', 0)) for c in candles_1h]
                volumes = [float(c.get('volume', 0)) for c in candles_1h]
                
                # Calculate ADX values for history
                from src.shared.indicators import calculate_adx
                adx_values_arr, _ = calculate_adx(
                    np.array(highs), np.array(lows), np.array(closes), 14
                )
                adx_values = [float(v) if not np.isnan(v) else 15.0 for v in adx_values_arr]
                
                # Warm start with historical data
                self._trend_intelligence.warm_start(
                    highs=highs,
                    lows=lows,
                    closes=closes,
                    volumes=volumes,
                    adx_values=adx_values
                )
                self._trend_phase = self._trend_intelligence.state.current_phase
            else:
                logger.warning(f"⚠️ Not enough 1h candles for TrendIntelligence warm start ({len(candles_1h) if candles_1h else 0} < 20)")
        except Exception as e:
            logger.warning(f"⚠️ TrendIntelligence warm start error: {e}")
        
        # 7. Log intelligence modules status
        logger.info("=" * 60)
        logger.info("🧠 INTELLIGENCE MODULES STATUS")
        logger.info("=" * 60)
        logger.info(f"📅 Daily Regime: {self._daily_regime} | Config: {self._daily_regime_config}")
        logger.info(f"📈 Trend Intelligence: {self._trend_phase.value}")
        logger.info(f"   Phase Detection: BIRTH → ACCEL → MATURE → EXHAUST → DEATH")
        logger.info(f"   Current: {self._trend_intelligence.get_phase_description()}")
        logger.info("=" * 60)
            
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
        logger.info("🔄 _main_loop: Starting main trading loop")
        last_reconcile_time = datetime.now(UTC)
        last_position_sync_time = datetime.now(UTC)
        last_heartbeat_time = datetime.now(UTC)
        tick_count = 0
        
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
                # FIX #5 (Jan 21, 2026): Added timeout to prevent blocking
                # FIX #6 (Jan 22, 2026): Skip if circuit breaker is open
                # ═══════════════════════════════════════════════════════════════
                if self._runtime_config and self._runtime_config.mode == RuntimeMode.LIVE:
                    time_since_sync = (datetime.now(UTC) - last_position_sync_time).total_seconds()
                    # Skip if circuit breaker active (exchange is down)
                    cb_open = hasattr(self.execution, 'is_circuit_breaker_open') and self.execution.is_circuit_breaker_open()
                    if time_since_sync > 30 and not cb_open:
                        logger.debug(f"🔄 Starting _periodic_reconciliation (last sync {time_since_sync:.0f}s ago)")
                        try:
                            await asyncio.wait_for(self._periodic_reconciliation(), timeout=15.0)
                            logger.debug("✅ _periodic_reconciliation completed")
                        except asyncio.TimeoutError:
                            logger.error("⏰ _periodic_reconciliation TIMEOUT (15s) - API may be slow")
                        except Exception as e:
                            logger.error(f"Error in periodic reconciliation: {e}")
                        last_position_sync_time = datetime.now(UTC)
                
                # Periodic order reconciliation (every 60s) - skip if circuit breaker open
                time_since_reconcile = (datetime.now(UTC) - last_reconcile_time).total_seconds()
                cb_open = hasattr(self.execution, 'is_circuit_breaker_open') and self.execution.is_circuit_breaker_open()
                if time_since_reconcile > 60 and not cb_open:
                    # Use enhanced reconciliation
                    try:
                        if hasattr(self.execution, 'auto_reconcile_orders'):
                            await asyncio.wait_for(self.execution.auto_reconcile_orders(self.symbol), timeout=15.0)
                        else:
                            await asyncio.wait_for(self._reconcile_orders(), timeout=15.0)
                    except asyncio.TimeoutError:
                        logger.error("⏰ Order reconciliation TIMEOUT (15s) - API may be slow")
                    except Exception as e:
                        logger.error(f"Error in order reconciliation: {e}")
                    last_reconcile_time = datetime.now(UTC)
                
                tick_start = datetime.now(UTC)
                tick_count += 1
                
                # 💓 HEARTBEAT: Log every 60 seconds to confirm system is alive
                time_since_heartbeat = (tick_start - last_heartbeat_time).total_seconds()
                if time_since_heartbeat >= 60:
                    pos = self.risk_engine.position
                    pos_status = "FLAT" if pos is None else f"{pos.direction} {pos.size:.6f}"
                    logger.info(f"💓 HEARTBEAT: tick #{tick_count}, state={self.state_machine.state.name}, pos={pos_status}")
                    
                    # 🤖 ML STATUS (every 5 minutes = 300s = tick_count % 300 == 0)
                    if tick_count % 300 == 0:
                        ml_stats = self._ml_inference.get_stats()
                        logger.info(f"🤖 ML STATUS: phase={ml_stats['phase']}, predictions={ml_stats['prediction_count']}, "
                                    f"failures={ml_stats['model_failures']}, avg_latency={ml_stats['avg_latency_ms']:.1f}ms, "
                                    f"drift={'⚠️ YES' if ml_stats['drift_detected'] else 'NO'}")
                        
                        # 💰 ACCOUNT REPORT (every 5 minutes - Jan 28, 2026)
                        try:
                            current_capital = self.risk_engine.capital
                            session_start = self._session_start_capital or current_capital
                            session_pnl = current_capital - session_start
                            session_pnl_pct = (session_pnl / session_start * 100) if session_start > 0 else 0.0
                            session_duration = "N/A"
                            if self._session_start_time:
                                duration_sec = (tick_start - self._session_start_time).total_seconds()
                                hours = int(duration_sec // 3600)
                                minutes = int((duration_sec % 3600) // 60)
                                session_duration = f"{hours}h{minutes}m"
                            
                            # Get daily PnL from state machine if available
                            daily_pnl = getattr(self.state_machine, '_daily_pnl', 0.0)
                            
                            pnl_emoji = "🟢" if session_pnl >= 0 else "🔴"
                            logger.info(f"💰 ACCOUNT REPORT: Balance=${current_capital:.2f} | "
                                        f"Session {pnl_emoji} ${session_pnl:+.2f} ({session_pnl_pct:+.2f}%) | "
                                        f"Daily P&L=${daily_pnl:+.2f} | "
                                        f"Duration={session_duration}")
                        except Exception as acc_err:
                            logger.debug(f"Account report error: {acc_err}")
                    
                    last_heartbeat_time = tick_start
                
                # 🔒 FIX (Jan 21, 2026): Add global timeout to _process_tick
                # Prevents infinite blocking if any async call hangs
                # 🔒 FIX (Jan 22, 2026): Use shield + wait to properly cancel subtasks
                tick_task = None
                try:
                    tick_task = asyncio.create_task(self._process_tick())
                    await asyncio.wait_for(asyncio.shield(tick_task), timeout=30.0)
                except asyncio.TimeoutError:
                    logger.error("⏰ _process_tick TIMEOUT (30s) - cancelling subtasks...")
                    if tick_task and not tick_task.done():
                        tick_task.cancel()
                        try:
                            # Give cancelled task time to cleanup (max 2s)
                            await asyncio.wait_for(tick_task, timeout=2.0)
                        except (asyncio.CancelledError, asyncio.TimeoutError):
                            pass  # Expected
                    # Reset exchange session if hanging (likely stuck CCXT call)
                    try:
                        await self.execution._reset_exchange_session()
                        logger.info("🔄 Exchange session reset after timeout")
                    except Exception as reset_err:
                        logger.warning(f"Could not reset exchange session: {reset_err}")
                    
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
            # Initialize on first call (can't use hasattr since we init as None)
            if self._last_reconcile_time is None:
                self._last_reconcile_time = now
            time_since_reconcile = (now - self._last_reconcile_time).total_seconds()
            if time_since_reconcile >= 5:
                try:
                    # Add 10s timeout to prevent infinite blocking
                    await asyncio.wait_for(self.reconciler.reconcile(), timeout=10.0)
                    self._last_reconcile_time = now
                except asyncio.TimeoutError:
                    logger.error("⏰ Reconciler TIMEOUT (10s) - API may be slow or stuck")
                    self._last_reconcile_time = now  # Reset to avoid spam
                except Exception as e:
                    logger.error(f"Reconciler error: {e}")
        
        # NOTE: _enforce_exchange_state_truth() removed - reconciler already handles this
        # and calling both caused redundant API calls (4 second ticks)
        
        # Check if we have sufficient data
        has_data = self.market_data.has_complete_data()
        
        if not has_data:
            # Log state of data buffers (pero no cada iteración para evitar spam)
            if self._last_incomplete_log is None:
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
            candle_high=float(last_candle.get('high', 0) or 0),
            candle_low=float(last_candle.get('low', 0) or 0),
            candle_open=float(last_candle.get('open', 0) or 0),
            candle_close=float(last_candle.get('close', 0) or 0)
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
            if self._last_defensive_log is None or \
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
        # � REFACTORED (Jan 27, 2026): Using idle_state_handlers
        # =====================================================================
        
        # 1. REJECTION COOLDOWN CHECK
        in_cooldown, cooldown_reason = check_rejection_cooldown(self)
        if in_cooldown:
            self._idle_eval_count += 1
            if self._idle_eval_count % 30 == 0:
                logger.debug(f"⏸️ {cooldown_reason}")
            return
        
        # 2. CANDLE GATE CHECK (EVENT-BASED - Jan 28, 2026)
        # Now evaluates on:
        # - New candle close (always)
        # - Price movement >= 0.3 ATR
        # - Max 30 ticks without event
        should_eval, self._idle_eval_count = check_candle_gate(
            self, self.setup_state, self._idle_eval_count, current_price, atr_1m
        )
        if not should_eval:
            return
        
        # Log activity
        if self._idle_eval_count % 30 == 0:
            logger.info(f"📊 IDLE: Evaluated {self._idle_eval_count} ticks | Price: ${current_price:.2f} | ATR: {atr_1m:.2f}")
        
        # Reset trade tags for new evaluation
        self._current_trade_tags = {}
        
        # 3. PENDING STATE CONFLICTS CHECK
        conflict_state = check_pending_state_conflicts(self)
        if conflict_state == "WAIT_FILL":
            self.state_machine.transition_to(BotState.WAIT_FILL, "Recovered from IDLE with pending order")
            return
        elif conflict_state == "MANAGE":
            self.state_machine.transition_to(BotState.MANAGE, "Recovered from IDLE with open position")
            return
        
        # 4. EXCHANGE SYNC (every 30 seconds)
        should_return, transition_state = await sync_with_exchange_if_needed(self, market_time)
        if should_return and transition_state == "MANAGE":
            self.state_machine.transition_to(BotState.MANAGE, "Synced with exchange position")
            return
        
        # ===== 5. WAIT_LTF STATE HANDLER =====
        wait_ltf_result = await handle_wait_ltf_state(self, current_price, atr_1m, market_time)
        if wait_ltf_result == "EXECUTING":
            await self._execute_entry(current_price, atr_1m, market_time)
            return
        elif wait_ltf_result == "RESET":
            return  # Setup invalidated or timed out
        elif self.setup_state == "WAIT_LTF":
            return  # Still waiting, stay in WAIT_LTF
        
        # ===== 6. PRE-TRADE CHECKS =====
        can_trade, trade_reason = await check_pre_trade_conditions(self, market_time)
        if not can_trade:
            if self._idle_eval_count % 100 == 0:
                logger.info(f"⚠️ Cannot trade: {trade_reason}")
            return
        
        # ===== 7. BALANCE REFRESH (every 5 minutes) =====
        await refresh_balance_if_needed(self, market_time)
        
        # ===== 8. ANTICIPATION ENGINE =====
        anticipation_tags = check_anticipation_signal(self, current_price, market_time)
        self._current_trade_tags.update(anticipation_tags)
        
        # ===== 9. DAILY REGIME UPDATE (every 1 hour) =====
        update_daily_regime_if_needed(self, market_time)
        
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
            # 🔥 FIX (Jan 29, 2026 - AUDIT): Store OLD bias BEFORE updating
            old_bias = self._htf_cache_bias  # MUST be before .check() updates
            
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
            # 🔥 FIX (Jan 23, 2026): Cache HTF regime mode based on ADX
            self._htf_cache_regime_mode = self.htf_filter.last_htf_regime_mode
            # 🔥 FIX (Jan 25, 2026): Ensure ADX cache is always a Python float
            _raw_adx = self.htf_filter.last_htf_adx
            self._htf_cache_adx = float(_raw_adx) if hasattr(_raw_adx, '__float__') else float(_raw_adx) if _raw_adx else 0.0
            logger.debug(f"HTF cache updated: ts={last_1h_ts}, passed={htf_passed}, bias={bias}, regime_mode={self._htf_cache_regime_mode}, adx={self._htf_cache_adx:.1f}")
            
            # 🔥 FIX (Jan 29, 2026 - AUDIT): Reset ONLY when bias ACTUALLY changes
            # Also reset expansion/direction lock state on bias change
            if old_bias != bias and old_bias is not None:
                logger.info(f"📊 BIAS CHANGED: {old_bias}→{bias} - resetting displacement and expansion state")
                
                # Reset expansion mode
                self._expansion_mode = False
                self._expansion_direction = None
                self._expansion_start_time = None
                self._expansion_initial_displacement = 0.0
                
                # Reset direction lock
                self._direction_lock = None
                self._direction_lock_reason = None
                self._direction_lock_expires_at = None
                self._direction_lock_initial_move = 0.0
                
                # Reset forbidden direction
                self._forbidden_direction = None
            
            # Always reset price tracking on new HTF candle
            self._bias_established_price = current_price
            self._bias_established_time = market_time
            self._bias_high_since = current_price
            self._bias_low_since = current_price
            
            # =================================================================
            # 🔥 TREND INTELLIGENCE UPDATE (Jan 23, 2026)
            # Update trend phase detection on each new 1h candle
            # This detects trend BIRTH, ACCELERATION, EXHAUSTION, DEATH
            # =================================================================
            try:
                last_candle = candles_1h[-1]
                atr_1h_arr = calculate_atr(
                    df_1h['high'].to_numpy(), df_1h['low'].to_numpy(), df_1h['close'].to_numpy(), 14
                )
                atr_1h = float(atr_1h_arr[-1]) if len(atr_1h_arr) > 0 else 100.0
                
                trend_state = self._trend_intelligence.update(
                    current_price=current_price,
                    high=float(last_candle.get('high', current_price)),
                    low=float(last_candle.get('low', current_price)),
                    close=float(last_candle.get('close', current_price)),
                    adx=self._htf_cache_adx,
                    volume=float(last_candle.get('volume', 0)),
                    atr=atr_1h,
                    timestamp=market_time
                )
                
                # Update phase tracking
                old_phase = self._trend_phase
                self._trend_phase = trend_state.current_phase
                
                if old_phase != self._trend_phase:
                    logger.info(
                        f"📈 TREND PHASE: {self._trend_intelligence.get_phase_description()}"
                    )
                    
                # Store for trade tags
                self._current_trade_tags['trend_phase'] = self._trend_phase.value
                self._current_trade_tags['trend_direction'] = trend_state.direction.value
                self._current_trade_tags['trend_entry_mode'] = trend_state.entry_mode
                self._current_trade_tags['trend_bias'] = trend_state.trade_bias
                
            except Exception as e:
                logger.debug(f"TrendIntelligence update error: {e}")
        else:
            # Usar caché
            htf_passed = self._htf_cache_passed
            bias = self._htf_cache_bias
            htf_reason = self._htf_cache_reason
        
        # =====================================================================
        # 🔥 FIX (Jan 28, 2026): BIAS ARCHITECTURE REWRITE
        # 
        # PROBLEMA ORIGINAL: BIAS INVALIDATION = BLOQUEO TOTAL (deadlock)
        # - LONG invalidado → NEUTRAL → no LONG ni SHORT
        # - Eso es INCORRECTO. Invalidar LONG debe PERMITIR buscar SHORT.
        #
        # SOLUCIÓN: Separar conceptos:
        # 1. STRUCTURAL_BIAS: HTF bias (no bloquea, solo informa)
        # 2. ACTIVE_DIRECTION: Qué buscar AHORA (basado en momentum)
        # 3. FORBIDDEN_DIRECTION: Dirección bloqueada por invalidación
        # 4. DISPLACEMENT_BYPASS: Si hay movimiento fuerte, bypass filters
        # =====================================================================
        
        # Store STRUCTURAL_BIAS (HTF bias - informativo, no bloqueante)
        self._structural_bias = bias  # Can be 'LONG', 'SHORT', or None
        self._current_trade_tags['structural_bias'] = self._structural_bias
        
        # Update price tracking for displacement detection
        if self._bias_high_since > 0:
            self._bias_high_since = max(self._bias_high_since, current_price)
            self._bias_low_since = min(self._bias_low_since, current_price)
        else:
            # Initialize on first run
            self._bias_high_since = current_price
            self._bias_low_since = current_price
            self._bias_established_price = current_price
        
        # Get ATR for displacement/invalidation thresholds
        candles_1m_for_atr = self.market_data.get_candles('1m')
        atr_1m = current_price * 0.003  # Default 0.3%
        if candles_1m_for_atr and len(candles_1m_for_atr) >= 14:
            try:
                df_1m_temp = pd.DataFrame(candles_1m_for_atr[-30:])
                atr_vals = calculate_atr(
                    df_1m_temp['high'].to_numpy(),
                    df_1m_temp['low'].to_numpy(),
                    df_1m_temp['close'].to_numpy(),
                    14
                )
                if len(atr_vals) > 0:
                    atr_1m = float(atr_vals[-1])
            except Exception as e:
                # 🔥 FIX (Jan 29, 2026 - AUDIT BUG 13): Log exception instead of silencing
                logger.debug(f"ATR 1m calculation failed, using default: {e}")
        
        # Get ATR 5m for stronger displacement signal
        atr_5m = atr_1m * 2.0  # Default approximation
        atr_5m_avg = atr_5m  # For volatility ratio calculation
        candles_5m_for_atr = self.market_data.get_candles('5m')
        if candles_5m_for_atr and len(candles_5m_for_atr) >= 14:
            try:
                df_5m_temp = pd.DataFrame(candles_5m_for_atr[-30:])
                atr_5m_vals = calculate_atr(
                    df_5m_temp['high'].to_numpy(),
                    df_5m_temp['low'].to_numpy(),
                    df_5m_temp['close'].to_numpy(),
                    14
                )
                if len(atr_5m_vals) > 0:
                    atr_5m = float(atr_5m_vals[-1])
                    # Calculate average ATR for volatility ratio
                    if len(atr_5m_vals) >= 20:
                        atr_5m_avg = float(np.mean(atr_5m_vals[-20:]))
                    else:
                        atr_5m_avg = float(np.mean(atr_5m_vals))
            except Exception:
                pass
        
        # =====================================================================
        # 🌊 BE WATER: ADAPTIVE PARAMETERS (Jan 29, 2026)
        # 
        # Los timeouts y thresholds ya NO son fijos - escalan con volatilidad
        # 
        # Volatility ratio = ATR_actual / ATR_promedio
        # - ratio > 2.0: Flash crash/pump - timeouts CORTOS (reaccionar rápido)
        # - ratio 1.0-2.0: Normal - timeouts BASE
        # - ratio < 0.5: Chop lento - timeouts LARGOS (no whipsaw)
        #
        # Esto permite que el sistema sea FLUIDO como el agua:
        # - Se adapta a mercados rápidos
        # - Se adapta a mercados lentos
        # - Nunca se "pega" en estados fijos
        # =====================================================================
        self._current_volatility_ratio = atr_5m / atr_5m_avg if atr_5m_avg > 0 else 1.0
        
        # ADAPTIVE EXPANSION TIMEOUT
        # Alta vol (2x) → 22 min (rápido)
        # Normal (1x) → 45 min (base)
        # Baja vol (0.5x) → 90 min (lento)
        vol_timeout_mult = 1.0 / max(0.5, min(2.0, self._current_volatility_ratio))
        self._expansion_timeout_minutes = int(self._expansion_timeout_base * vol_timeout_mult)
        self._expansion_timeout_minutes = max(15, min(90, self._expansion_timeout_minutes))  # Floor 15, cap 90
        
        # ADAPTIVE DIRECTION LOCK TIMEOUT
        self._direction_lock_timeout_minutes = int(self._direction_lock_timeout_base * vol_timeout_mult)
        self._direction_lock_timeout_minutes = max(10, min(60, self._direction_lock_timeout_minutes))  # Floor 10, cap 60
        
        # ADAPTIVE ASYMMETRIC INVALIDATION
        # RANGE: más fácil invalidar (2.0x)
        # TREND: normal (3.0x)
        # EXPANSION: difícil invalidar (4.0x)
        # + Ajuste por volatilidad (alta vol → más duro invalidar)
        regime_mult = 1.0
        if self._expansion_mode:
            regime_mult = 1.33  # 4.0/3.0 = 1.33
        elif hasattr(self, '_htf_cache_adx') and self._htf_cache_adx < 18:
            regime_mult = 0.67  # 2.0/3.0 = 0.67 (RANGE)
        
        vol_inv_adj = min(0.5, max(-0.5, (self._current_volatility_ratio - 1.0) * 0.25))
        self._asymmetric_invalidation_mult = self._asymmetric_invalidation_base * regime_mult + vol_inv_adj
        self._asymmetric_invalidation_mult = max(1.5, min(5.0, self._asymmetric_invalidation_mult))  # Floor 1.5, cap 5.0
        
        # Log adaptive parameters periodically
        if self._idle_eval_count % 60 == 1:
            logger.info(
                f"🌊 ADAPTIVE PARAMS: vol_ratio={self._current_volatility_ratio:.2f} | "
                f"exp_timeout={self._expansion_timeout_minutes}min | "
                f"lock_timeout={self._direction_lock_timeout_minutes}min | "
                f"inv_mult={self._asymmetric_invalidation_mult:.2f}x"
            )
        
        self._current_trade_tags['volatility_ratio'] = self._current_volatility_ratio
        self._current_trade_tags['adaptive_exp_timeout'] = self._expansion_timeout_minutes
        self._current_trade_tags['adaptive_inv_mult'] = self._asymmetric_invalidation_mult
        
        # =====================================================================
        # 🔥 FIX 2: BIAS INVALIDATION → ALLOW OPPOSITE DIRECTION
        # 
        # ANTES: invalidate_bias() → BIAS = NEUTRAL (deadlock)
        # AHORA: invalidate_bias(LONG) → forbid LONG, ALLOW SHORT detection
        # =====================================================================
        
        # 🔥 FIX (Jan 29, 2026): Initialize tracking variables BEFORE calculating displacement
        # This prevents negative displacement values on system startup
        if self._bias_high_since == 0.0:
            self._bias_high_since = current_price
            self._bias_low_since = current_price
            logger.debug(f"📍 BIAS TRACKING INITIALIZED: high/low = ${current_price:.0f}")
        
        drop_from_high = self._bias_high_since - current_price
        rise_from_low = current_price - self._bias_low_since
        
        # =====================================================================
        # 🔥 DIRECTION_LOCK (Jan 29, 2026) - Lock direccional post-DISPLACEMENT
        # 
        # Separación OBLIGATORIA: MarketBias ≠ TradePermission
        # BIAS puede cambiar libremente, pero LOCK bloquea cambios de dirección
        #
        # Durante LOCK:
        # ❌ No bias flip contra la dirección lockeada
        # ❌ No forbid por retrocesos pequeños
        # ✅ Solo invalidación MAYOR (3x displacement inicial)
        # =====================================================================
        
        # Check DIRECTION_LOCK timeout
        # 🔥 FIX (Jan 29, 2026): Si EXPANSION_MODE está activo, extender el lock
        if self._direction_lock and self._direction_lock_expires_at:
            if datetime.now() >= self._direction_lock_expires_at:
                # Check if EXPANSION_MODE is still active - extend lock if so
                # 🔥 FIX (Jan 29, 2026 - AUDIT BUG 5): Add null check for _expansion_direction
                if self._expansion_mode and self._expansion_direction and self._expansion_direction == self._direction_lock:
                    # Extend lock by 15 min while expansion is active
                    self._direction_lock_expires_at = datetime.now() + timedelta(minutes=15)
                    logger.info(
                        f"⏰ DIRECTION_LOCK EXTENDED: {self._direction_lock} | "
                        f"EXPANSION_MODE still active - extending lock 15min"
                    )
                    self._current_trade_tags['direction_lock_extended'] = True
                else:
                    logger.warning(
                        f"⏰ DIRECTION_LOCK EXPIRED: {self._direction_lock} | "
                        f"Reason: {self._direction_lock_reason} | Initial move: ${self._direction_lock_initial_move:.0f}"
                    )
                    self._direction_lock = None
                    self._direction_lock_reason = None
                    self._direction_lock_expires_at = None
                    self._direction_lock_initial_move = 0.0
                    self._current_trade_tags['direction_lock_expired'] = True
        
        # Check for ASYMMETRIC INVALIDATION of DIRECTION_LOCK
        # Necesita 3x la evidencia para invalidar que para confirmar
        if self._direction_lock and self._direction_lock_initial_move > 0:
            opposite_move = 0.0
            if self._direction_lock == 'SHORT':
                opposite_move = rise_from_low
            elif self._direction_lock == 'LONG':
                opposite_move = drop_from_high
            
            # INVALIDACIÓN ASIMÉTRICA: Necesita 3x para invalidar (no 0.6x)
            asymmetric_threshold = self._direction_lock_initial_move * self._asymmetric_invalidation_mult
            if opposite_move >= asymmetric_threshold:
                logger.warning(
                    f"🔴 DIRECTION_LOCK INVALIDATED (ASYMMETRIC): Opposite move ${opposite_move:.0f} ≥ "
                    f"{self._asymmetric_invalidation_mult:.1f}x initial ${self._direction_lock_initial_move:.0f} = ${asymmetric_threshold:.0f} | "
                    f"Lock was {self._direction_lock} - NOW RELEASED"
                )
                self._direction_lock = None
                self._direction_lock_reason = None
                self._direction_lock_expires_at = None
                self._direction_lock_initial_move = 0.0
                self._current_trade_tags['direction_lock_invalidated_asymmetric'] = True
        
        # =====================================================================
        # 🔥 FIX (Jan 29, 2026): EXPANSION_MODE STATE MACHINE
        # 
        # Jerarquía de señales: DISPLACEMENT > BIAS FLIP > micro signals
        #
        # EXPANSION_MODE se activa cuando:
        # - STRONG DISPLACEMENT detectado (≥1.5x threshold)
        # - Marca el inicio de un movimiento direccional claro
        #
        # Durante EXPANSION_MODE:
        # - _forbidden_direction SUSPENDIDO (no bloquea dirección de expansion)
        # - BIAS FLIP threshold aumentado a 3x (muy difícil invalidar)
        # - Solo se desactiva por:
        #   1. Displacement opuesto ≥60% del inicial (ahora 3x con asimetría)
        #   2. Timeout (45 minutos)
        #   3. HTF structure break explícito
        # =====================================================================
        
        # Check EXPANSION_MODE timeout
        if self._expansion_mode and self._expansion_start_time:
            expansion_elapsed = (datetime.now() - self._expansion_start_time).total_seconds() / 60
            if expansion_elapsed >= self._expansion_timeout_minutes:
                logger.warning(
                    f"⏰ EXPANSION_MODE TIMEOUT: {expansion_elapsed:.1f}min elapsed ≥ {self._expansion_timeout_minutes}min | "
                    f"Direction: {self._expansion_direction} | Initial displacement: ${self._expansion_initial_displacement:.0f}"
                )
                self._expansion_mode = False
                self._expansion_direction = None
                self._expansion_start_time = None
                self._expansion_initial_displacement = 0.0
                self._current_trade_tags['expansion_timeout'] = True
        
        # Check for OPPOSITE displacement that would invalidate EXPANSION_MODE
        # 🔥 INVALIDACIÓN ASIMÉTRICA ADAPTATIVA: Necesita más evidencia para invalidar
        if self._expansion_mode and self._expansion_initial_displacement > 0:
            opposite_displacement = 0.0
            if self._expansion_direction == 'SHORT':
                # In SHORT expansion, check for RISE that invalidates
                opposite_displacement = rise_from_low
            elif self._expansion_direction == 'LONG':
                # In LONG expansion, check for DROP that invalidates
                opposite_displacement = drop_from_high
            
            # 🔥 ADAPTIVE ASYMMETRIC INVALIDATION (Jan 29, 2026)
            # Base: 3.0x, pero se ajusta por:
            # - Volatilidad alta → +0.5x (más difícil invalidar en moves fuertes)
            # - Volatilidad baja → -0.5x (más fácil invalidar en chop)
            # - Time decay → -0.05x por cada 10 min (máx -0.5x)
            # Floor de 2.0x para nunca ser demasiado fácil de invalidar
            adaptive_mult = self._asymmetric_invalidation_mult  # Base 3.0
            elapsed_min = 0.0  # Default
            
            # Volatility adjustment
            vol_ratio = atr_5m / atr_1m if atr_1m > 0 else 1.0
            if vol_ratio > 2.0:
                adaptive_mult += 0.5  # Alta volatilidad = más difícil invalidar
            elif vol_ratio < 1.2:
                adaptive_mult -= 0.5  # Baja volatilidad = más fácil invalidar
            
            # Time decay (hasta -0.5x en 100 min)
            if self._expansion_start_time:
                elapsed_min = (datetime.now() - self._expansion_start_time).total_seconds() / 60
                time_decay = min(0.5, elapsed_min * 0.005)
                adaptive_mult -= time_decay
            
            # Floor de 2.0x
            adaptive_mult = max(2.0, adaptive_mult)
            
            asymmetric_invalidation_threshold = self._expansion_initial_displacement * adaptive_mult
            if opposite_displacement >= asymmetric_invalidation_threshold:
                logger.warning(
                    f"🔴 EXPANSION_MODE INVALIDATED (ASYMMETRIC): Opposite ${opposite_displacement:.0f} ≥ "
                    f"{adaptive_mult:.1f}x initial ${self._expansion_initial_displacement:.0f} = ${asymmetric_invalidation_threshold:.0f} | "
                    f"(base=3.0, vol_adj={vol_ratio:.1f}, decay={elapsed_min:.0f}min) | "
                    f"Exiting {self._expansion_direction} expansion"
                )
                self._expansion_mode = False
                self._expansion_direction = None
                self._expansion_start_time = None
                self._expansion_initial_displacement = 0.0
                # Also clear direction lock if exists
                self._direction_lock = None
                self._direction_lock_reason = None
                self._direction_lock_expires_at = None
                self._direction_lock_initial_move = 0.0
                self._current_trade_tags['expansion_invalidated_asymmetric'] = True
                self._current_trade_tags['invalidation_mult_used'] = adaptive_mult
        
        # 🔥 FIX (Jan 29, 2026): ADAPTIVE BIAS FLIP THRESHOLD
        # Problema: Durante movimientos fuertes, pequeños rebotes (~$150-350) 
        # invalidaban direcciones válidas. El threshold de 1.5x ATR_1m era
        # demasiado sensible durante alta volatilidad.
        #
        # Solución: Aumentar threshold dinámicamente basado en:
        # 1. Si estamos en EXPANSION_MODE → threshold muy alto (3x)
        # 2. Volatilidad actual (ATR_5m vs ATR_1m) → threshold alto (2x)
        # 3. Normal → threshold base (1.5x)
        # =====================================================================
        BASE_INVALIDATION_ATR_MULT = 1.5  # Base threshold
        
        # EXPANSION_MODE: Usar threshold muy alto - muy difícil invalidar
        if self._expansion_mode:
            INVALIDATION_ATR_MULT = 3.0  # Durante expansion, 3x threshold
            logger.debug(f"EXPANSION_MODE active ({self._expansion_direction}) - BIAS FLIP threshold 3x")
        else:
            # Aumentar threshold si la volatilidad 5m es alta
            volatility_ratio = atr_5m / atr_1m if atr_1m > 0 else 1.0
            if volatility_ratio > 1.5:
                # Alta volatilidad - usar threshold más alto para evitar whipsaws
                INVALIDATION_ATR_MULT = 2.0
            else:
                INVALIDATION_ATR_MULT = BASE_INVALIDATION_ATR_MULT
        
        invalidation_threshold = atr_1m * INVALIDATION_ATR_MULT
        
        # Track invalidation level for restoration
        if not hasattr(self, '_bias_invalidation_level'):
            self._bias_invalidation_level: float = 0.0
            self._bias_before_invalidation: str | None = None
        
        # 🔥 FIX: Only log when state CHANGES (avoid spam)
        _previous_forbidden = self._forbidden_direction
        self._forbidden_direction = None  # Reset each tick
        
        # 🔥 EXPANSION_MODE PROTECTION: During expansion, don't forbid the expansion direction
        # This prevents small bounces from blocking valid trades in the expansion direction
        expansion_protected_direction = self._expansion_direction if self._expansion_mode else None
        
        # 🔥 CORRECTED LOGIC (Jan 29, 2026): Una sola evaluación con protección integrada
        if bias == 'LONG' and drop_from_high > invalidation_threshold:
            # LONG invalidated by price drop → normally FORBID LONG
            # 🔥 EXPANSION_MODE + DIRECTION_LOCK PROTECTION for LONG
            direction_locked_long = self._direction_lock == 'LONG'
            
            if expansion_protected_direction == 'LONG' or direction_locked_long:
                # PROTECTION CASE: We're in LONG expansion/lock - don't forbid
                protection_reason = 'DIRECTION_LOCK' if direction_locked_long else 'EXPANSION_MODE'
                if _previous_forbidden != 'LONG' and self._idle_eval_count % 30 == 1:
                    logger.info(
                        f"🛡️ {protection_reason} PROTECTED: Drop ${drop_from_high:.0f} would FORBID LONG, "
                        f"but {protection_reason}=LONG active - ignoring dip"
                    )
                self._current_trade_tags['protected_dip'] = True
                self._current_trade_tags['protection_reason'] = protection_reason
                # DON'T set forbidden_direction - protection active
            else:
                # FORBID CASE: Normal bias flip
                self._forbidden_direction = 'LONG'
                self._bias_invalidation_level = self._bias_high_since - invalidation_threshold * 0.5
                self._bias_before_invalidation = 'LONG'
                # Only log when state changes
                if _previous_forbidden != 'LONG':
                    logger.warning(
                        f"⚠️ BIAS FLIP: LONG invalidated (drop ${drop_from_high:.0f} > {invalidation_threshold:.0f}) "
                        f"→ FORBID LONG, SHORT detection ENABLED | Reclaim level: ${self._bias_invalidation_level:.0f}"
                    )
                self._current_trade_tags['bias_invalidated'] = True
                self._current_trade_tags['forbidden_direction'] = 'LONG'
                self._current_trade_tags['reclaim_level'] = self._bias_invalidation_level
            
        elif bias == 'SHORT' and rise_from_low > invalidation_threshold:
            # SHORT invalidated by price rise → normally FORBID SHORT
            # 🔥 EXPANSION_MODE PROTECTION: If we're in SHORT expansion, DON'T forbid SHORT
            # 🔒 DIRECTION_LOCK PROTECTION: If locked to SHORT, DON'T forbid SHORT
            # The bounce is likely a pullback in the expansion, not a reversal
            direction_locked_short = self._direction_lock == 'SHORT'
            
            if expansion_protected_direction == 'SHORT' or direction_locked_short:
                # PROTECTION CASE: We're in SHORT expansion/lock - don't forbid
                protection_reason = 'DIRECTION_LOCK' if direction_locked_short else 'EXPANSION_MODE'
                if _previous_forbidden != 'SHORT' and self._idle_eval_count % 30 == 1:
                    logger.info(
                        f"🛡️ {protection_reason} PROTECTED: Rise ${rise_from_low:.0f} would FORBID SHORT, "
                        f"but {protection_reason}=SHORT active - ignoring bounce"
                    )
                self._current_trade_tags['protected_bounce'] = True
                self._current_trade_tags['protection_reason'] = protection_reason
                # DON'T set forbidden_direction - protection active
            else:
                # FORBID CASE: Normal bias flip
                self._forbidden_direction = 'SHORT'
                self._bias_invalidation_level = self._bias_low_since + invalidation_threshold * 0.5
                self._bias_before_invalidation = 'SHORT'
                # Only log when state changes
                if _previous_forbidden != 'SHORT':
                    logger.warning(
                        f"⚠️ BIAS FLIP: SHORT invalidated (rise ${rise_from_low:.0f} > {invalidation_threshold:.0f}) "
                        f"→ FORBID SHORT, LONG detection ENABLED | Reclaim level: ${self._bias_invalidation_level:.0f}"
                    )
                self._current_trade_tags['bias_invalidated'] = True
                self._current_trade_tags['forbidden_direction'] = 'SHORT'
                self._current_trade_tags['reclaim_level'] = self._bias_invalidation_level
        
        # =====================================================================
        # 🔥 FIX 2b: BIAS RESTORATION (reversible invalidation)
        # 
        # La invalidación NO puede ser permanente - debe ser contextual y reversible.
        # Si el precio recupera el nivel clave + momentum direccional → restaurar bias
        # =====================================================================
        if self._bias_before_invalidation and self._bias_invalidation_level > 0:
            momentum_ok = False
            reclaim_ok = False
            
            # Check if price reclaimed the level
            if self._bias_before_invalidation == 'LONG':
                reclaim_ok = current_price >= self._bias_invalidation_level
            elif self._bias_before_invalidation == 'SHORT':
                reclaim_ok = current_price <= self._bias_invalidation_level
            
            # Check momentum (2 closes in the right direction)
            if reclaim_ok:
                try:
                    candles_1m_check = self.market_data.get_candles('1m')
                    if candles_1m_check and len(candles_1m_check) >= 3:
                        last_3_closes = [c['close'] for c in candles_1m_check[-3:]]
                        last_3_opens = [c['open'] for c in candles_1m_check[-3:]]
                        
                        if self._bias_before_invalidation == 'LONG':
                            # Need 2 bullish closes
                            bullish_count = sum(1 for i in range(len(last_3_closes)) 
                                                if last_3_closes[i] > last_3_opens[i])
                            momentum_ok = bullish_count >= 2
                        else:
                            # Need 2 bearish closes
                            bearish_count = sum(1 for i in range(len(last_3_closes)) 
                                                if last_3_closes[i] < last_3_opens[i])
                            momentum_ok = bearish_count >= 2
                except Exception:
                    pass
            
            # RESTORE BIAS if conditions met
            if reclaim_ok and momentum_ok:
                restored_bias = self._bias_before_invalidation
                logger.info(
                    f"🔄 BIAS RESTORED: {restored_bias} | Price reclaimed ${self._bias_invalidation_level:.0f} "
                    f"+ momentum confirmed | Clearing forbidden direction"
                )
                bias = restored_bias
                self._structural_bias = restored_bias
                self._forbidden_direction = None
                self._bias_invalidation_level = 0.0
                self._bias_before_invalidation = None
                self._current_trade_tags['bias_restored'] = True
                self._current_trade_tags['bias_restored_direction'] = restored_bias
        
        # =====================================================================
        # 🔥 FIX 3: FLIP AUTOMÁTICO POR DISPLACEMENT
        # 
        # Si el precio se mueve ≥1.2×ATR_5m en una dirección:
        # → ACTIVE_DIRECTION = dirección del movimiento
        # → El mercado YA DECIDIÓ, no esperar HTF
        #
        # 🔥 FIX (Jan 29, 2026): DISPLACEMENT OVERRIDES BIAS FLIP
        # Cuando hay displacement fuerte (≥1.5x threshold), el mercado está
        # en movimiento direccional claro. En este caso:
        # - IGNORAR _forbidden_direction completamente
        # - El pequeño rebote que causó el BIAS FLIP es ruido
        # - Confiar en el momentum institucional del displacement
        # =====================================================================
        
        # 🌊 BE WATER: ADAPTIVE DISPLACEMENT THRESHOLDS
        # Alta volatilidad (2x) → threshold más alto (evitar ruido)
        # Baja volatilidad (0.5x) → threshold más bajo (capturar micromoves)
        BASE_DISPLACEMENT_ATR_MULT = 1.2  # Base threshold
        vol_displacement_adj = 1.0 + (self._current_volatility_ratio - 1.0) * 0.3  # +/- 30% por volatilidad
        vol_displacement_adj = max(0.7, min(1.5, vol_displacement_adj))  # Floor 0.7, cap 1.5
        
        DISPLACEMENT_ATR_MULT = BASE_DISPLACEMENT_ATR_MULT * vol_displacement_adj
        displacement_threshold = atr_5m * DISPLACEMENT_ATR_MULT
        
        # 🌊 BE WATER: STRONG DISPLACEMENT también adaptativo
        # En mercados rápidos, necesitamos más confirmación
        STRONG_DISPLACEMENT_MULT = 1.5 * vol_displacement_adj  # Escala con volatilidad
        strong_displacement_threshold = displacement_threshold * STRONG_DISPLACEMENT_MULT
        
        self._displacement_bypass = False
        self._strong_displacement = False  # New flag for STRONG displacement
        
        # Store for diagnostics
        self._current_trade_tags['displacement_threshold'] = displacement_threshold
        self._current_trade_tags['strong_disp_threshold'] = strong_displacement_threshold
        self._current_trade_tags['vol_displacement_adj'] = vol_displacement_adj
        
        # Calculate displacement from session prices
        if drop_from_high >= displacement_threshold:
            # Strong downward displacement → ACTIVE_DIRECTION = SHORT
            self._active_direction = 'SHORT'
            self._displacement_bypass = True
            
            # 🔥 STRONG DISPLACEMENT → ACTIVATE EXPANSION_MODE + DIRECTION_LOCK
            if drop_from_high >= strong_displacement_threshold:
                self._strong_displacement = True
                
                # 🔥 EXPANSION_MODE ACTIVATION: Only activate if not already in expansion
                # or if this is a stronger displacement in the same direction
                if not self._expansion_mode or self._expansion_direction != 'SHORT':
                    self._expansion_mode = True
                    self._expansion_direction = 'SHORT'
                    self._expansion_start_time = datetime.now()
                    self._expansion_initial_displacement = drop_from_high
                    
                    # 🔒 DIRECTION_LOCK ACTIVATION: Lock direccional post-DISPLACEMENT
                    self._direction_lock = 'SHORT'
                    self._direction_lock_reason = 'STRONG_DISPLACEMENT'
                    self._direction_lock_expires_at = datetime.now() + timedelta(minutes=self._direction_lock_timeout_minutes)
                    self._direction_lock_initial_move = drop_from_high
                    
                    logger.warning(
                        f"🚀 EXPANSION_MODE + DIRECTION_LOCK ACTIVATED: SHORT | "
                        f"Initial displacement ${drop_from_high:.0f} ≥ {strong_displacement_threshold:.0f} | "
                        f"BIAS FLIP threshold now 3x | Lock: {self._direction_lock_timeout_minutes}min | "
                        f"INVALIDATION requires ${drop_from_high * self._asymmetric_invalidation_mult:.0f} opposite (3x)"
                    )
                    self._current_trade_tags['expansion_mode_activated'] = True
                    self._current_trade_tags['expansion_direction'] = 'SHORT'
                    self._current_trade_tags['direction_lock'] = 'SHORT'
                    
                    # =========================================================
                    # 🌊 FIX CRÍTICO (Jan 30, 2026): ARM DISPLACEMENT_EDGE
                    # 
                    # PROBLEMA: EXPANSION_MODE + DIRECTION_LOCK funcionaban
                    # pero NO generaban EDGE → 0 trades en caída de 2.3%
                    # 
                    # SOLUCIÓN: Armar DISPLACEMENT_EDGE automáticamente
                    # El displacement ES el edge - no necesita más confirmación
                    # =========================================================
                    displacement_edge_armed = self._edge_manager.arm_displacement_edge(
                        current_price=current_price,
                        direction='SHORT',
                        displacement_amount=drop_from_high,
                        atr=atr_5m,
                        market_time=market_time,
                        initial_price=self._bias_high_since
                    )
                    if displacement_edge_armed:
                        self._current_trade_tags['displacement_edge_armed'] = True
                        self._current_trade_tags['edge_type'] = 'DISPLACEMENT'
                        logger.info(
                            f"🌊 DISPLACEMENT EDGE ARMED: SHORT | "
                            f"Displacement ${drop_from_high:.0f} | Initial ${self._bias_high_since:.0f}"
                        )
                
                # Clear forbidden direction - displacement wins
                if self._forbidden_direction == 'SHORT':
                    logger.info(
                        f"🔥 DISPLACEMENT OVERRIDE BIAS FLIP: Down ${drop_from_high:.0f} ≥ {strong_displacement_threshold:.0f} | "
                        f"Clearing FORBIDDEN=SHORT - momentum wins"
                    )
                    self._forbidden_direction = None
                    self._current_trade_tags['displacement_override_bias_flip'] = True
            
            if self._idle_eval_count % 30 == 1:
                logger.info(
                    f"🔥 DISPLACEMENT DETECTED: Down ${drop_from_high:.0f} ≥ {displacement_threshold:.0f} "
                    f"→ ACTIVE_DIRECTION=SHORT | Bypass filters enabled"
                    f"{' | STRONG DISPLACEMENT' if self._strong_displacement else ''}"
                    f"{' | EXPANSION_MODE' if self._expansion_mode else ''}"
                )
            self._current_trade_tags['displacement_direction'] = 'SHORT'
            self._current_trade_tags['displacement_amount'] = drop_from_high
            self._current_trade_tags['strong_displacement'] = self._strong_displacement
            
        elif rise_from_low >= displacement_threshold:
            # Strong upward displacement → ACTIVE_DIRECTION = LONG
            self._active_direction = 'LONG'
            self._displacement_bypass = True
            
            # 🔥 STRONG DISPLACEMENT → ACTIVATE EXPANSION_MODE + DIRECTION_LOCK
            if rise_from_low >= strong_displacement_threshold:
                self._strong_displacement = True
                
                # 🔥 EXPANSION_MODE ACTIVATION: Only activate if not already in expansion
                # or if this is a stronger displacement in the same direction
                if not self._expansion_mode or self._expansion_direction != 'LONG':
                    self._expansion_mode = True
                    self._expansion_direction = 'LONG'
                    self._expansion_start_time = datetime.now()
                    self._expansion_initial_displacement = rise_from_low
                    
                    # 🔒 DIRECTION_LOCK ACTIVATION: Lock direccional post-DISPLACEMENT
                    self._direction_lock = 'LONG'
                    self._direction_lock_reason = 'STRONG_DISPLACEMENT'
                    self._direction_lock_expires_at = datetime.now() + timedelta(minutes=self._direction_lock_timeout_minutes)
                    self._direction_lock_initial_move = rise_from_low
                    
                    logger.warning(
                        f"🚀 EXPANSION_MODE + DIRECTION_LOCK ACTIVATED: LONG | "
                        f"Initial displacement ${rise_from_low:.0f} ≥ {strong_displacement_threshold:.0f} | "
                        f"BIAS FLIP threshold now 3x | Lock: {self._direction_lock_timeout_minutes}min | "
                        f"INVALIDATION requires ${rise_from_low * self._asymmetric_invalidation_mult:.0f} opposite (3x)"
                    )
                    self._current_trade_tags['expansion_mode_activated'] = True
                    self._current_trade_tags['expansion_direction'] = 'LONG'
                    self._current_trade_tags['direction_lock'] = 'LONG'
                    
                    # =========================================================
                    # 🌊 FIX CRÍTICO (Jan 30, 2026): ARM DISPLACEMENT_EDGE
                    # 
                    # PROBLEMA: EXPANSION_MODE + DIRECTION_LOCK funcionaban
                    # pero NO generaban EDGE → 0 trades
                    # 
                    # SOLUCIÓN: Armar DISPLACEMENT_EDGE automáticamente
                    # El displacement ES el edge - no necesita más confirmación
                    # =========================================================
                    displacement_edge_armed = self._edge_manager.arm_displacement_edge(
                        current_price=current_price,
                        direction='LONG',
                        displacement_amount=rise_from_low,
                        atr=atr_5m,
                        market_time=market_time,
                        initial_price=self._bias_low_since
                    )
                    if displacement_edge_armed:
                        self._current_trade_tags['displacement_edge_armed'] = True
                        self._current_trade_tags['edge_type'] = 'DISPLACEMENT'
                        logger.info(
                            f"🌊 DISPLACEMENT EDGE ARMED: LONG | "
                            f"Displacement ${rise_from_low:.0f} | Initial ${self._bias_low_since:.0f}"
                        )
                
                # Clear forbidden direction - displacement wins
                if self._forbidden_direction == 'LONG':
                    logger.info(
                        f"🔥 DISPLACEMENT OVERRIDE BIAS FLIP: Up ${rise_from_low:.0f} ≥ {strong_displacement_threshold:.0f} | "
                        f"Clearing FORBIDDEN=LONG - momentum wins"
                    )
                    self._forbidden_direction = None
                    self._current_trade_tags['displacement_override_bias_flip'] = True
            
            if self._idle_eval_count % 30 == 1:
                logger.info(
                    f"🔥 DISPLACEMENT DETECTED: Up ${rise_from_low:.0f} ≥ {displacement_threshold:.0f} "
                    f"→ ACTIVE_DIRECTION=LONG | Bypass filters enabled"
                    f"{' | STRONG DISPLACEMENT' if self._strong_displacement else ''}"
                    f"{' | EXPANSION_MODE' if self._expansion_mode else ''}"
                )
            self._current_trade_tags['displacement_direction'] = 'LONG'
            self._current_trade_tags['displacement_amount'] = rise_from_low
            self._current_trade_tags['strong_displacement'] = self._strong_displacement
            
        else:
            # No displacement - reset flags
            self._strong_displacement = False
            
            # =====================================================================
            # 🔥 FIX 6 (Jan 28, 2026): DETECCIÓN DINÁMICA DE MOMENTUM (OPTIMIZADO)
            # 
            # Problema: Sin displacement grande, el sistema usa HTF bias que
            # puede estar equivocado respecto al movimiento actual.
            #
            # Solución: Detectar momentum MICRO usando últimas velas 1m
            # 
            # OPTIMIZACIÓN LATENCIA:
            # - Usar numpy diff() en lugar de loops
            # - Solo recalcular cada 3 ticks (no cada tick)
            # - Cache del resultado
            # =====================================================================
            micro_momentum = None
            
            # Solo recalcular cada 3 evaluaciones para reducir latencia
            should_calc_micro = (self._idle_eval_count % 3 == 0) or not hasattr(self, '_cached_micro_momentum')
            
            if should_calc_micro:
                try:
                    candles_1m_recent = self.market_data.get_candles('1m')
                    if candles_1m_recent and len(candles_1m_recent) >= 10:
                        # Usar numpy para eficiencia
                        recent_highs = np.array([c['high'] for c in candles_1m_recent[-10:]])
                        recent_lows = np.array([c['low'] for c in candles_1m_recent[-10:]])
                        recent_closes = np.array([c['close'] for c in candles_1m_recent[-10:]])
                        recent_opens = np.array([c['open'] for c in candles_1m_recent[-10:]])
                        recent_volumes = np.array([c.get('volume', 1.0) for c in candles_1m_recent[-10:]])
                        
                        # Calcular diferencias con numpy (más rápido que loops)
                        high_diffs = np.diff(recent_highs)
                        low_diffs = np.diff(recent_lows)
                        close_diffs = np.diff(recent_closes)
                        
                        # Contar HH/HL/LL/LH (últimas 7 velas)
                        hh_count = np.sum(high_diffs[-6:] > 0)
                        hl_count = np.sum(low_diffs[-6:] > 0)
                        ll_count = np.sum(low_diffs[-6:] < 0)
                        lh_count = np.sum(high_diffs[-6:] < 0)
                        
                        bullish_score = hh_count + hl_count
                        bearish_score = ll_count + lh_count
                        
                        # Clear momentum requires 4+ points in one direction
                        if bullish_score >= 4 and bullish_score > bearish_score + 2:
                            micro_momentum = 'LONG'
                        elif bearish_score >= 4 and bearish_score > bullish_score + 2:
                            micro_momentum = 'SHORT'
                        
                        # =========================================================
                        # 🔥 INTELLIGENT: MOVE QUALITY SCORE
                        # 
                        # Determinar si estamos en:
                        # - INICIO del movimiento (entrar con size completo)
                        # - MEDIO del movimiento (entrar con size reducido)
                        # - FINAL del movimiento (NO entrar, esperar pullback)
                        # =========================================================
                        move_quality = 'UNKNOWN'
                        move_size_mult = 1.0
                        
                        if micro_momentum:
                            # 1. BODY SIZE RATIO: Velas impulsivas vs correctivas
                            body_sizes = np.abs(recent_closes - recent_opens)
                            avg_body = np.mean(body_sizes[:-1])  # Promedio excluyendo actual
                            last_bodies = body_sizes[-3:]  # Últimas 3 velas
                            
                            # 2. VOLUME PROFILE: Volumen creciente = inicio, decreciente = final
                            vol_early = np.mean(recent_volumes[-6:-3])
                            vol_late = np.mean(recent_volumes[-3:])
                            vol_ratio = vol_late / vol_early if vol_early > 0 else 1.0
                            
                            # 3. CANDLE SHRINKAGE: Velas cada vez más pequeñas = exhaustion
                            shrinkage = last_bodies[-1] / np.mean(last_bodies[:-1]) if np.mean(last_bodies[:-1]) > 0 else 1.0
                            
                            # 4. TAIL/WICK ANALYSIS: Mechas largas = rechazo
                            if micro_momentum == 'LONG':
                                # En LONG, mechas superiores largas = rechazo
                                upper_wicks = recent_highs - np.maximum(recent_opens, recent_closes)
                                wick_ratio = upper_wicks[-1] / body_sizes[-1] if body_sizes[-1] > 0 else 0
                            else:
                                # En SHORT, mechas inferiores largas = rechazo
                                lower_wicks = np.minimum(recent_opens, recent_closes) - recent_lows
                                wick_ratio = lower_wicks[-1] / body_sizes[-1] if body_sizes[-1] > 0 else 0
                            
                            # DETERMINE MOVE QUALITY
                            if vol_ratio >= 1.0 and shrinkage >= 0.8 and wick_ratio < 0.5:
                                # Volumen creciente, velas fuertes, sin rechazo = INICIO
                                move_quality = 'FRESH'
                                move_size_mult = 1.0
                            elif vol_ratio >= 0.7 and shrinkage >= 0.5:
                                # Volumen ok, velas medianas = MEDIO
                                move_quality = 'DEVELOPING'
                                move_size_mult = 0.7
                            elif vol_ratio < 0.7 or shrinkage < 0.5 or wick_ratio >= 1.0:
                                # Volumen bajando o velas pequeñas o mechas de rechazo = FINAL
                                move_quality = 'EXHAUSTING'
                                move_size_mult = 0.0  # NO ENTRAR
                            
                            self._current_trade_tags['move_quality'] = move_quality
                            self._current_trade_tags['move_quality_size_mult'] = move_size_mult
                            self._current_trade_tags['move_vol_ratio'] = vol_ratio
                            self._current_trade_tags['move_shrinkage'] = shrinkage
                            self._current_trade_tags['move_wick_ratio'] = wick_ratio
                            
                            # Log when quality is poor
                            if move_quality == 'EXHAUSTING' and self._idle_eval_count % 30 == 1:
                                logger.info(
                                    f"⚠️ MOVE EXHAUSTING: {micro_momentum} momentum but vol_ratio={vol_ratio:.2f}, "
                                    f"shrinkage={shrinkage:.2f}, wick={wick_ratio:.2f} | WAIT FOR PULLBACK"
                                )
                        
                        # Verificar con closes (más simple y rápido)
                        up_closes = np.sum(close_diffs > 0)
                        if micro_momentum == 'LONG' and up_closes >= 4:
                            self._current_trade_tags['micro_momentum_strength'] = 'STRONG'
                        elif micro_momentum == 'SHORT' and up_closes <= 2:
                            self._current_trade_tags['micro_momentum_strength'] = 'STRONG'
                        elif micro_momentum:
                            self._current_trade_tags['micro_momentum_strength'] = 'WEAK'
                        
                        if micro_momentum:
                            self._current_trade_tags['micro_momentum'] = micro_momentum
                        
                        # Cachear resultado
                        self._cached_micro_momentum = micro_momentum
                            
                except Exception as e:
                    logger.debug(f"Micro momentum detection error: {e}")
            else:
                # Usar cache
                micro_momentum = getattr(self, '_cached_micro_momentum', None)
            
            # Determine ACTIVE_DIRECTION with momentum consideration
            if micro_momentum and micro_momentum != self._forbidden_direction:
                # MICRO momentum detected - use it if not forbidden
                self._active_direction = micro_momentum
                if self._idle_eval_count % 60 == 1:
                    logger.info(
                        f"🎯 MICRO MOMENTUM: {micro_momentum} detected | "
                        f"Structural={self._structural_bias} | Using MICRO for direction"
                    )
            elif self._structural_bias and self._structural_bias != self._forbidden_direction:
                self._active_direction = self._structural_bias
            elif self._forbidden_direction == 'LONG':
                self._active_direction = 'SHORT'  # Only SHORT allowed
            elif self._forbidden_direction == 'SHORT':
                self._active_direction = 'LONG'  # Only LONG allowed
            else:
                self._active_direction = self._structural_bias  # Use HTF
        
        self._current_trade_tags['active_direction'] = self._active_direction
        self._current_trade_tags['displacement_bypass'] = self._displacement_bypass
        
        # Now use active_direction instead of bias for trading
        bias = self._active_direction  # This is what we'll trade
        
        # =====================================================================
        # IMPORTANT: NO MORE DEADLOCK
        # Even if structural_bias was LONG and got invalidated:
        # - forbidden_direction = 'LONG' 
        # - active_direction = 'SHORT' (can still trade)
        # - NO return statement, we continue to look for SHORT setups
        # =====================================================================
        
        # =====================================================================
        # 🔥 FIX (Jan 23, 2026): RANGE MODE - NOT BLOCK, USE DIFFERENT RULES
        # When ADX < 18, we are in RANGE/CHOP - NOT trending
        # 
        # CORRECCIÓN CRÍTICA: No bloquear, usar MODO RANGE con reglas:
        #   - Trades SÍ permitidos en EXTREMOS (VAH/VAL/VWAP bands)
        #   - Size reducido (50%)
        #   - Stop más tight (2x ATR vs 3x)
        #   - TP corto + salida rápida
        #   - Score threshold más alto (7.0 vs 6.0)
        # 
        # El rango es donde BTC pasa el 60-70% del tiempo - no podemos
        # ignorarlo, hay que explotarlo correctamente.
        # =====================================================================
        htf_regime_mode = self._htf_cache_regime_mode
        htf_adx = self._htf_cache_adx
        
        # =====================================================================
        # 🔥 FIX (Jan 23, 2026): FAST REGIME DETECTION using 5m ADX
        # Problem: Using only 1h ADX meant we could be 1 HOUR late detecting
        # regime changes. An intraday bot needs faster detection.
        # 
        # Solution: Use 5m ADX for FAST detection, 1h ADX for CONFIRMATION
        #   - If 5m ADX drops < 15 → IMMEDIATE RANGE MODE (don't wait for 1h)
        #   - If 5m ADX > 25 AND 1h ADX > 18 → STRONG TREND
        #   - Otherwise, use 1h ADX as primary
        # =====================================================================
        fast_adx = htf_adx  # Default to 1h
        candles_5m = self.market_data.get_candles('5m')
        if candles_5m and len(candles_5m) >= 30:
            try:
                df_5m_temp = pd.DataFrame(candles_5m[-30:])  # Last 30 candles = 2.5 hours
                adx_5m_vals, _ = calculate_adx(
                    df_5m_temp['high'].to_numpy(), 
                    df_5m_temp['low'].to_numpy(), 
                    df_5m_temp['close'].to_numpy(), 
                    14
                )
                if len(adx_5m_vals) > 0 and not np.isnan(adx_5m_vals[-1]):
                    fast_adx = float(adx_5m_vals[-1])
                    self._current_trade_tags['adx_5m_fast'] = fast_adx
            except Exception as e:
                logger.debug(f"Fast ADX calc error: {e}")
        
        # =====================================================================
        # 🔥 FIX 4 (Jan 28, 2026): PRIORIDAD TEMPORAL PARA INTRADAY
        # 
        # PROBLEMA: effective_adx = min(fast_adx, htf_adx) es LETAL para intraday
        # - ADX 5m = 62.5 (TREND fuerte en 5m)
        # - ADX 1h = 20.7 (1h no confirma)
        # - min() = 20.7 → RANGE (¡PERDEMOS EL MOVIMIENTO!)
        #
        # SOLUCIÓN: Prioridad TEMPORAL (5m domina para intraday)
        # - Si ADX 5m ≥ 35 → TREND (no esperar 1h)
        # - Si ADX 1h ≥ 25 → TREND (1h confirma)
        # - Else → RANGE
        # 
        # ❌ NUNCA usar min()
        # ❌ NUNCA dejar que el 1h mate al 5m intraday
        # =====================================================================
        
        # Calculate effective ADX with TEMPORAL PRIORITY
        if fast_adx >= 35:
            # 5m shows STRONG TREND - this IS the trend, don't wait for 1h
            effective_adx = fast_adx  # Trust the faster timeframe
            if self._idle_eval_count % 60 == 1:
                logger.info(
                    f"⚡ FAST TREND: ADX 5m={fast_adx:.1f} ≥ 35 → Using 5m for trend detection"
                )
        elif htf_adx >= 25:
            # 1h confirms trend - use 1h
            effective_adx = htf_adx
        elif fast_adx >= 25:
            # 5m trending but not strong - blend but favor 5m for intraday
            effective_adx = fast_adx * 0.6 + htf_adx * 0.4
        else:
            # Both weak - use 1h as base
            effective_adx = htf_adx
        
        # Store regime for downstream use
        self._current_trade_tags['htf_regime_mode'] = htf_regime_mode
        self._current_trade_tags['htf_adx'] = htf_adx
        self._current_trade_tags['adx_5m'] = fast_adx
        self._current_trade_tags['effective_adx'] = effective_adx
        
        # =====================================================================
        # 🔥 FIX (Jan 28, 2026): TREND INTELLIGENCE SOVEREIGNTY
        # 
        # PROBLEMA: ADX dice TREND (>20) pero TrendIntelligence dice RANGE_BOUND
        # Esto crea "zona muerta" donde nada funciona.
        #
        # SOLUCIÓN: TrendIntelligence tiene soberanía sobre el régimen
        # - Si TrendIntelligence = RANGE_BOUND → forzar RANGE mode
        # - Si TrendIntelligence = TRENDING → puede ser TREND mode
        # - ADX solo sirve para ajustar agresividad DENTRO del modo
        #
        # TrendIntelligence usa más señales (price action, volume, momentum)
        # ADX es solo un indicador, no el juez final
        # =====================================================================
        
        # PHASE 1: Start with ADX-based mode (legacy)
        if effective_adx < 18:
            adx_suggested_mode = 'RANGE'
        elif effective_adx < 20:
            adx_suggested_mode = 'TRANSITION'
        else:
            adx_suggested_mode = 'TREND'
        
        # PHASE 2: TrendIntelligence override (sovereignty)
        trend_phase = getattr(self, '_trend_phase', TrendPhase.RANGE_BOUND)
        trend_phase_str = trend_phase.value if hasattr(trend_phase, 'value') else str(trend_phase)
        
        # =====================================================================
        # 🔥 FIX (Jan 28): DISPLACEMENT OVERRIDES TREND INTELLIGENCE
        # If we detected strong displacement, we're in a BREAKOUT, not RANGE
        # TrendIntelligence is lagging - displacement is real-time
        # =====================================================================
        if self._displacement_bypass:
            # Strong displacement = active move = TREND mode, not RANGE
            trading_mode = 'TREND'
            if self._idle_eval_count % 30 == 1:
                logger.info(
                    f"🔥 DISPLACEMENT OVERRIDE: TrendPhase={trend_phase_str} but displacement active → TREND mode"
                )
        # TrendIntelligence says RANGE_BOUND → force RANGE mode regardless of ADX
        elif trend_phase_str == 'RANGE_BOUND':
            trading_mode = 'RANGE'
            if adx_suggested_mode != 'RANGE' and self._idle_eval_count % 60 == 1:
                logger.info(
                    f"🎯 TREND INTELLIGENCE OVERRIDE: ADX says {adx_suggested_mode} but phase is RANGE_BOUND → RANGE"
                )
        # TrendIntelligence says ACCUMULATION → TRANSITION (could break either way)
        elif trend_phase_str == 'ACCUMULATION':
            trading_mode = 'TRANSITION'
        # TrendIntelligence in active trend → respect ADX for intensity
        elif trend_phase_str in ('TREND_BIRTH', 'ACCELERATION', 'MATURE'):
            trading_mode = adx_suggested_mode if adx_suggested_mode == 'TREND' else 'TRANSITION'
        # TrendIntelligence says exhaustion/death → conservative
        elif trend_phase_str in ('EXHAUSTION', 'TREND_DEATH'):
            trading_mode = 'RANGE'  # Don't fight dying trends
        else:
            # Fallback to ADX-based mode
            trading_mode = adx_suggested_mode
        
        self._current_trade_tags['trading_mode'] = trading_mode
        self._current_trade_tags['adx_suggested_mode'] = adx_suggested_mode
        self._current_trade_tags['trend_phase'] = trend_phase_str
        
        # =====================================================================
        # 🔥 NEW (Jan 28, 2026): INTRADAY EDGE DATA COLLECTION
        # Calculate data needed for INTRADAY edge detection in entry_orchestrator
        # =====================================================================
        try:
            candles_1m = self.market_data.get_candles('1m')
            if candles_1m and len(candles_1m) >= 30:
                df_1m_intraday = pd.DataFrame(candles_1m[-30:])
                
                # ATR current vs average (for expansion ratio)
                atr_1m_arr = calculate_atr(
                    df_1m_intraday['high'].to_numpy(),
                    df_1m_intraday['low'].to_numpy(),
                    df_1m_intraday['close'].to_numpy(),
                    14
                )
                if len(atr_1m_arr) >= 10:
                    atr_1m_current = float(atr_1m_arr[-1])
                    # 🔥 FIX (Jan 29, 2026 - AUDIT BUG 7): Proper bounds check for slice
                    if len(atr_1m_arr) >= 21:
                        atr_1m_avg = float(np.mean(atr_1m_arr[-20:-1]))  # Average of last 20 excluding current
                    else:
                        # Not enough data for full slice - use available data
                        atr_1m_avg = float(np.mean(atr_1m_arr[:-1])) if len(atr_1m_arr) > 1 else atr_1m_current
                    self._current_trade_tags['atr_1m'] = atr_1m_current
                    self._current_trade_tags['atr_1m_avg'] = atr_1m_avg
                    self._current_trade_tags['atr_expansion_ratio'] = atr_1m_current / atr_1m_avg if atr_1m_avg > 0 else 1.0
                
                # Volume current vs average
                if 'volume' in df_1m_intraday.columns:
                    vol_current = float(df_1m_intraday['volume'].iloc[-1])
                    vol_avg = float(df_1m_intraday['volume'].iloc[:-1].mean())
                    self._current_trade_tags['volume_current'] = vol_current
                    self._current_trade_tags['volume_average'] = vol_avg
                    self._current_trade_tags['volume_ratio'] = vol_current / vol_avg if vol_avg > 0 else 1.0
                
                # Swing direction (HH/HL = BULLISH, LL/LH = BEARISH)
                highs = df_1m_intraday['high'].to_numpy()[-10:]
                lows = df_1m_intraday['low'].to_numpy()[-10:]
                
                # Find swing points (simple: local highs and lows)
                hh_count = 0
                ll_count = 0
                for i in range(2, len(highs)):
                    if highs[i] > highs[i-1] and highs[i-1] > highs[i-2]:
                        hh_count += 1
                    if lows[i] > lows[i-1] and lows[i-1] > lows[i-2]:
                        hh_count += 1  # Higher lows also bullish
                    if lows[i] < lows[i-1] and lows[i-1] < lows[i-2]:
                        ll_count += 1
                    if highs[i] < highs[i-1] and highs[i-1] < highs[i-2]:
                        ll_count += 1  # Lower highs also bearish
                
                if hh_count > ll_count + 1:
                    swing_dir = 'BULLISH'
                    swing_count = hh_count
                elif ll_count > hh_count + 1:
                    swing_dir = 'BEARISH'
                    swing_count = ll_count
                else:
                    swing_dir = 'NEUTRAL'
                    swing_count = 0
                
                self._current_trade_tags['swing_direction'] = swing_dir
                self._current_trade_tags['swing_count'] = swing_count
                
        except Exception as e:
            logger.debug(f"INTRADAY data collection error: {e}")
            # Set defaults
            self._current_trade_tags.setdefault('atr_1m', 100.0)
            self._current_trade_tags.setdefault('atr_1m_avg', 100.0)
            self._current_trade_tags.setdefault('volume_current', 1.0)
            self._current_trade_tags.setdefault('volume_average', 1.0)
            self._current_trade_tags.setdefault('swing_direction', 'NEUTRAL')
            self._current_trade_tags.setdefault('swing_count', 0)
        
        # Log mode changes with full context (rate limited)
        if self._idle_eval_count % 30 == 1:
            override_note = ""
            if adx_suggested_mode != trading_mode:
                override_note = f" ⚡ OVERRIDE from {adx_suggested_mode}"
            
            # Include displacement info if active
            displacement_note = ""
            if self._displacement_bypass:
                displacement_note = f" | 🔥 DISPLACEMENT={self._active_direction}"
            
            # Include forbidden direction if any
            forbidden_note = ""
            if self._forbidden_direction:
                forbidden_note = f" | ⛔ FORBIDDEN={self._forbidden_direction}"
            
            logger.info(
                f"🎯 TRADING MODE: {trading_mode}{override_note} | "
                f"TrendPhase={trend_phase_str} | "
                f"ADX[1h={htf_adx:.1f}, 5m={fast_adx:.1f}, eff={effective_adx:.1f}] | "
                f"Structural={self._structural_bias} Active={self._active_direction}"
                f"{displacement_note}{forbidden_note}"
            )
        
        # ===================================================================
        # RANGE MODE RULES (ADX < 18)
        # Not a block - different entry rules for ranging markets
        # 🔥 FIX (Jan 23): MUST have location_context, otherwise NO TRADE
        # 🔥 FIX (Jan 24): Calculate location_context EARLY if in RANGE mode
        # 🔥 FIX (Jan 30): SKIP RANGE ANALYSIS when DISPLACEMENT EDGE is active
        # ===================================================================
        skip_range_analysis = False  # 🔥 FIX (Jan 30)
        if trading_mode == 'RANGE':
            # 🔥 DISPLACEMENT EDGE KILLS RANGE ANALYSIS (Jan 30, 2026)
            # When DISPLACEMENT edge is active, RANGE analysis is NOISE
            # The market already broke out of range - don't confuse the system
            has_displacement_edge_active = (
                hasattr(self, '_edge_manager') and
                self._edge_manager.state == EdgeState.EDGE_ACTIVE and
                self._edge_manager.current_bos is not None and
                self._edge_manager.current_bos.edge_type == EdgeType.DISPLACEMENT
            )
            if has_displacement_edge_active:
                # Skip ALL range analysis - displacement edge takes over
                skip_range_analysis = True
                if self._idle_eval_count % 60 == 1:
                    disp_dir = self._edge_manager.current_bos.direction if self._edge_manager.current_bos else 'UNKNOWN'
                    logger.info(
                        f"🌊 RANGE SKIPPED: DISPLACEMENT EDGE {disp_dir} is active | "
                        f"Range analysis disabled - market already broke range"
                    )
        
        # Only process RANGE rules if not skipping
        if trading_mode == 'RANGE' and not skip_range_analysis:
            # EARLY location_context calculation if missing
            # This fixes timing bug where we check before calculating
            if not self._location_context:
                try:
                    closes_1h_early = np.asarray(df_1h['close'].to_numpy())
                    highs_1h_early = np.asarray(df_1h['high'].to_numpy())
                    lows_1h_early = np.asarray(df_1h['low'].to_numpy())
                    volumes_1h_early = np.asarray(df_1h['volume'].to_numpy()) if 'volume' in df_1h.columns else np.ones(len(closes_1h_early))
                    
                    atr_1h_vals_early = calculate_atr(highs_1h_early, lows_1h_early, closes_1h_early, 14)
                    atr_1h_early = atr_1h_vals_early[-1] if len(atr_1h_vals_early) > 0 else current_price * 0.01
                    
                    adx_1h_vals_early, _ = calculate_adx(highs_1h_early, lows_1h_early, closes_1h_early, 14)
                    adx_1h_early = adx_1h_vals_early[-1] if len(adx_1h_vals_early) > 0 else 20.0
                    
                    location_cache_key_early = _ts_to_int(df_1h.iloc[-1].get('timestamp', 0))
                    _, _, self._location_context = self.location_filter.check(
                        closes_1h=closes_1h_early,
                        highs_1h=highs_1h_early,
                        lows_1h=lows_1h_early,
                        volumes_1h=volumes_1h_early,
                        current_price=current_price,
                        atr_1h=atr_1h_early,
                        adx_1h=adx_1h_early,
                        bias=bias,
                        regime='RANGE',
                        cache_key=location_cache_key_early
                    )
                    logger.debug(f"📍 RANGE MODE: Calculated early location_context")
                except Exception as e:
                    logger.warning(f"⚠️ RANGE MODE: Failed to calculate early location_context: {e}")
            
            # In RANGE: Only trade at EXTREMES (VAH/VAL/VWAP deviation)
            # Check if we're at an extreme using location context
            at_extreme = False
            extreme_type = None
            
            # 🔥 FIX: If no location_context, we can't determine if at extreme
            # In RANGE mode without context = NO TRADE (conservative)
            # 🔥 FIX (Jan 29, 2026): DON'T BLOCK if DISPLACEMENT/EXPANSION is active!
            if not self._location_context:
                # Check if we should bypass due to displacement
                if self._displacement_bypass or self._expansion_mode:
                    logger.info(
                        f"🔥 RANGE MODE: No location_context but DISPLACEMENT active | "
                        f"BYPASSING range check - displacement takes priority"
                    )
                    # Don't return - continue to trade
                else:
                    if self._idle_eval_count % 120 == 1:
                        logger.info(
                            f"⏸️ RANGE MODE: No location context available | "
                            f"Cannot determine if at extreme - SKIPPING"
                        )
                    return
            
            pct_60d = self._location_context.percentile_60d if self._location_context else 50.0
            self._current_trade_tags['percentile_60d'] = pct_60d
            
            # 🌊 BE WATER: ADAPTIVE VOLUME THRESHOLD
            # En volatilidad alta, volumen importa más (necesitamos confirmación)
            # En volatilidad baja, podemos aceptar menos volumen (mercado lento)
            BASE_MIN_VOLUME_RATIO = 0.5  # Base: 50% of average
            vol_volume_adj = 1.0 + (self._current_volatility_ratio - 1.0) * 0.2  # +/- 20%
            vol_volume_adj = max(0.7, min(1.3, vol_volume_adj))
            MIN_VOLUME_RATIO_RANGE = BASE_MIN_VOLUME_RATIO * vol_volume_adj
            MIN_VOLUME_RATIO_RANGE = max(0.3, min(0.7, MIN_VOLUME_RATIO_RANGE))  # Floor 30%, cap 70%
            
            volume_ratio = self._current_trade_tags.get('volume_ratio', 1.0)
            if volume_ratio < MIN_VOLUME_RATIO_RANGE:
                # Check displacement bypass
                if self._displacement_bypass or self._expansion_mode:
                    logger.info(
                        f"🔥 RANGE VOLUME BYPASS: vol_ratio={volume_ratio:.0%} < {MIN_VOLUME_RATIO_RANGE:.0%} | "
                        f"But DISPLACEMENT active - proceeding"
                    )
                else:
                    if self._idle_eval_count % 60 == 1:
                        logger.info(
                            f"🚫 HARD BLOCK: RANGE + volumen bajo | "
                            f"vol_ratio={volume_ratio:.0%} < {MIN_VOLUME_RATIO_RANGE:.0%} | NO TRADE"
                        )
                    return  # No trading in dead market
            
            # 🌊 BE WATER: ADAPTIVE EXTREME PERCENTILES
            # En volatilidad alta, los extremos se alcanzan más fácil → más estrictos (10/90)
            # En volatilidad baja, ajustar → menos estrictos (20/80)
            # Esto permite capturar más oportunidades en mercados lentos
            BASE_EXTREME_HIGH_PCT = 85
            BASE_EXTREME_LOW_PCT = 15
            
            # Volatility adjustment: alta vol → estricto, baja vol → flexible
            vol_extreme_adj = (self._current_volatility_ratio - 1.0) * 5  # +/- 5 percentile points
            vol_extreme_adj = max(-5, min(5, vol_extreme_adj))
            
            EXTREME_HIGH_PCT = BASE_EXTREME_HIGH_PCT + vol_extreme_adj  # 80-90
            EXTREME_LOW_PCT = BASE_EXTREME_LOW_PCT - vol_extreme_adj  # 10-20
            
            # Ensure valid bounds
            EXTREME_HIGH_PCT = max(80, min(95, EXTREME_HIGH_PCT))
            EXTREME_LOW_PCT = max(5, min(20, EXTREME_LOW_PCT))
            
            self._current_trade_tags['adaptive_extreme_high'] = EXTREME_HIGH_PCT
            self._current_trade_tags['adaptive_extreme_low'] = EXTREME_LOW_PCT
            
            if pct_60d >= EXTREME_HIGH_PCT:
                at_extreme = True
                extreme_type = 'HIGH'  # Only SHORT allowed here
                if bias == 'LONG':
                    # 🔥 FIX (Jan 29, 2026): Don't block LONG if we have DISPLACEMENT or EXPANSION
                    # This allows ATH breakouts to be traded
                    if self._displacement_bypass or self._expansion_mode:
                        logger.info(
                            f"🔥 RANGE HIGH BYPASS: pct={pct_60d:.0f}% (HIGH) but DISPLACEMENT/EXPANSION active | "
                            f"Allowing LONG - potential ATH breakout"
                        )
                        self._current_trade_tags['range_high_bypass'] = True
                    else:
                        logger.debug(f"⛔ RANGE MODE: At {pct_60d:.0f}% (HIGH≥{EXTREME_HIGH_PCT:.0f}%) - LONG blocked, only SHORT")
                        return  # Can't long at the top in range (unless displacement)
            elif pct_60d <= EXTREME_LOW_PCT:
                at_extreme = True
                extreme_type = 'LOW'  # Only LONG allowed here
                if bias == 'SHORT':
                    # 🔥 FIX (Jan 29, 2026): Don't block SHORT if we have DISPLACEMENT or EXPANSION
                    # This allows breakdown to be traded
                    if self._displacement_bypass or self._expansion_mode:
                        logger.info(
                            f"🔥 RANGE LOW BYPASS: pct={pct_60d:.0f}% (LOW) but DISPLACEMENT/EXPANSION active | "
                            f"Allowing SHORT - potential breakdown"
                        )
                        self._current_trade_tags['range_low_bypass'] = True
                    else:
                        logger.debug(f"⛔ RANGE MODE: At {pct_60d:.0f}% (LOW) - SHORT blocked, only LONG")
                        return  # Can't short at the bottom in range (unless displacement)
            
            if not at_extreme:
                # =========================================================================
                # 🔥 FIX 5 (Jan 28, 2026): RANGE BYPASS CUANDO HAY DISPLACEMENT
                # 
                # PROBLEMA: Cuando ocurre un drop violento en RANGE:
                # - El rango está siendo ROTO
                # - RANGE percentile ya no importa
                # - Pero HARD BLOCK sigue activo → perdemos el movimiento
                #
                # SOLUCIÓN: Si hay displacement fuerte O EXPANSION_MODE, bypass RANGE block
                # - displacement_bypass = True significa que el mercado decidió
                # - expansion_mode = True significa que estamos en un movimiento direccional
                # - No importa el percentile, el rango está siendo violado
                # =========================================================================
                if self._displacement_bypass or self._expansion_mode:
                    # Strong displacement or EXPANSION_MODE detected - RANGE is being broken
                    bypass_reason = 'expansion_mode' if self._expansion_mode else 'displacement'
                    logger.info(
                        f"🔥 RANGE BYPASS: {bypass_reason.upper()} | pct={pct_60d:.0f}% ignored | "
                        f"ACTIVE_DIRECTION={self._active_direction} | "
                        f"{'EXPANSION=' + str(self._expansion_direction) + ' | ' if self._expansion_mode else ''}"
                        f"Proceeding with trade"
                    )
                    self._current_trade_tags['range_bypass_reason'] = bypass_reason
                    self._current_trade_tags['range_size_mult'] = 0.60  # Slightly reduced size for range-break
                    # Don't return - continue to trade
                else:
                    # No displacement - enforce HARD BLOCK
                    # Regla profesional: En RANGE solo se entra en extremos.
                    # NO hay probing en rango medio - es trading a ciegas.
                    if self._idle_eval_count % 60 == 1:
                        logger.info(
                            f"🚫 HARD BLOCK: RANGE sin extremo | pct={pct_60d:.0f}% "
                            f"(need ≤{EXTREME_LOW_PCT}% or ≥{EXTREME_HIGH_PCT}%) | NO TRADE"
                        )
                    return  # HARD BLOCK - no exceptions (unless displacement)
            else:
                # At extreme in RANGE - apply RANGE rules with GRADUAL sizing
                self._current_trade_tags['range_mode_extreme'] = extreme_type
                
                # 🌊 BE WATER: GRADUAL SIZE based on percentile distance from extreme
                # At 15% (perfect extreme) → 60% size
                # At 20% (edge of extreme) → 40% size
                # Smooth interpolation between
                if extreme_type == 'LOW':
                    # Closer to 0% = better extreme
                    extreme_quality = (EXTREME_LOW_PCT - pct_60d) / EXTREME_LOW_PCT  # 1.0 at 0%, 0.0 at 15%
                else:
                    # Closer to 100% = better extreme
                    extreme_quality = (pct_60d - EXTREME_HIGH_PCT) / (100 - EXTREME_HIGH_PCT)  # 1.0 at 100%, 0.0 at 85%
                
                extreme_quality = max(0.0, min(1.0, extreme_quality))
                gradual_range_size = 0.40 + (extreme_quality * 0.20)  # 40% to 60%
                self._current_trade_tags['range_size_mult'] = gradual_range_size
                self._current_trade_tags['extreme_quality'] = extreme_quality
                
                logger.info(
                    f"🎯 RANGE TRADE: {bias} at {extreme_type} extreme (pct={pct_60d:.0f}%) | "
                    f"Quality={extreme_quality:.0%} | Size={gradual_range_size:.0%} | Stop=2xATR"
                )
        
        # ===================================================================
        # TRANSITION MODE RULES (ADX 18-20)
        # 🌊 BE WATER: GRADUAL SIZE based on ADX position within transition zone
        # ===================================================================
        elif trading_mode == 'TRANSITION':
            self._current_trade_tags['transition_mode'] = True
            
            # Gradual size: ADX 18 → 60%, ADX 20 → 80%, interpolate
            transition_progress = (effective_adx - 18) / 2.0  # 0.0 at 18, 1.0 at 20
            transition_progress = max(0.0, min(1.0, transition_progress))
            gradual_transition_size = 0.60 + (transition_progress * 0.20)  # 60% to 80%
            
            self._current_trade_tags['transition_size_mult'] = gradual_transition_size
            self._current_trade_tags['transition_progress'] = transition_progress
            self._current_trade_tags['transition_score_add'] = 0.5 * (1 - transition_progress)  # Less penalty near TREND
            
            if self._idle_eval_count % 30 == 1:
                logger.info(
                    f"⚠️ TRANSITION MODE: ADX={effective_adx:.1f} | "
                    f"Progress={transition_progress:.0%} | Size={gradual_transition_size:.0%}"
                )
        
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
            if self._last_htf_reject_log_time is None:
                self._last_htf_reject_log_time = market_time
            
            seconds_since_log = (market_time - self._last_htf_reject_log_time).total_seconds()
            
            # Log cada 5 minutos (300 segundos) o si cambió de razón
            should_log = (seconds_since_log >= 300) or (self._last_htf_state != 'rejected')
            
            if should_log:
                logger.info(f"[BLOCK] HTF filter: {htf_reason}")
                
                # Show detailed HTF values
                ema_50 = df_1h['close'].ewm(span=50, adjust=False).mean().iloc[-1]
                ema_200 = df_1h['close'].ewm(span=200, adjust=False).mean().iloc[-1]
                adx_vals, _ = calculate_adx(df_1h['high'].to_numpy(), df_1h['low'].to_numpy(), df_1h['close'].to_numpy(), 14)
                # 🔥 FIX (Jan 25, 2026): Ensure ADX is Python float
                adx = float(adx_vals[-1]) if len(adx_vals) > 0 else 0.0
                adx_slope = float(adx_vals[-1] - adx_vals[-2]) if len(adx_vals) > 1 else 0.0
                
                logger.debug(f"   [DATA] Price: ${current_price:.2f} | EMA50: ${ema_50:.2f} | EMA200: ${ema_200:.2f}")
                logger.debug(f"   [UP] ADX: {adx:.2f} | Slope: {adx_slope:.4f}")
                
                self._last_htf_reject_log_time = market_time
                self._last_htf_state = 'rejected'
            return
        
        self.current_bias = bias
        bias_label = f'{bias} (counter-trend)' if allow_countertrend else bias
        if self._last_htf_state != f'passed_{bias}':
            logger.debug(f"[OK] HTF filter passed - Bias: {bias_label}")
            self._last_htf_state = f'passed_{bias}'
        
        # =====================================================================
        # 🎯 EDGE STATE MANAGER (Jan 27, 2026) - Edge como EVENTO DISCRETO
        # 
        # El sistema SOLO evalúa entradas si hay un EDGE ACTIVO.
        # Sin edge → NO hay evaluación → NO hay overtrading basura.
        # 
        # Flujo:
        # 1. Detectar swings para BOS detection
        # 2. Check for new BOS (arma el edge)
        # 3. Check for pullback (activa el edge)
        # 4. Check expiration/invalidation
        # 5. Si no hay edge activo → skip evaluación
        # =====================================================================
        
        # Preparar datos para edge detection
        closes_1h = np.asarray(df_1h['close'].to_numpy())
        highs_1h = np.asarray(df_1h['high'].to_numpy())
        lows_1h = np.asarray(df_1h['low'].to_numpy())
        
        atr_1h_vals = calculate_atr(highs_1h, lows_1h, closes_1h, 14)
        atr_1h = float(atr_1h_vals[-1]) if len(atr_1h_vals) > 0 else current_price * 0.01
        
        # Detectar swings con lookback de 5 velas
        from src.shared.indicators import detect_swing_high, detect_swing_low
        
        swing_high_detected, swing_high_price, _ = detect_swing_high(highs_1h, lows_1h, closes_1h, lookback=5)
        swing_low_detected, swing_low_price, _ = detect_swing_low(highs_1h, lows_1h, closes_1h, lookback=5)
        
        # Actualizar swings si se detectaron nuevos
        if swing_high_detected and swing_high_price > 0:
            if swing_high_price != self._last_swing_high:
                self._prev_swing_high = self._last_swing_high
                self._last_swing_high = swing_high_price
        
        if swing_low_detected and swing_low_price > 0:
            if swing_low_price != self._last_swing_low:
                self._prev_swing_low = self._last_swing_low
                self._last_swing_low = swing_low_price
        
        # Inicializar swings si aún no están (usar highs/lows recientes)
        if self._last_swing_high == 0:
            self._last_swing_high = np.max(highs_1h[-10:]) if len(highs_1h) >= 10 else current_price
            self._prev_swing_high = np.max(highs_1h[-20:-10]) if len(highs_1h) >= 20 else self._last_swing_high
        if self._last_swing_low == 0:
            self._last_swing_low = np.min(lows_1h[-10:]) if len(lows_1h) >= 10 else current_price
            self._prev_swing_low = np.min(lows_1h[-20:-10]) if len(lows_1h) >= 20 else self._last_swing_low
        
        # 1. Check expiration/invalidation primero
        self._edge_manager.check_expiration(market_time)
        self._edge_manager.check_invalidation(current_price, atr_1h)
        
        # Import EdgeType for BREAKOUT direction override check
        from src.domain.trading.edge_state_manager import EdgeType
        
        # =====================================================================
        # 🌊 FIX (Jan 30, 2026): Update DISPLACEMENT entry window
        # 
        # Si hay DISPLACEMENT edge activo, actualizar la ventana de entrada
        # y matar el edge si está demasiado tarde (NO PERSEGUIR)
        # =====================================================================
        if self._edge_manager.current_bos and self._edge_manager.current_bos.edge_type == EdgeType.DISPLACEMENT:
            should_kill, kill_reason = self._edge_manager.update_displacement_entry_window(market_time)
            if should_kill:
                logger.warning(f"🚫 {kill_reason}")
                self._edge_manager._expire_edge(kill_reason)
                self._current_trade_tags['displacement_killed_late'] = True
        
        # =====================================================================
        # 🔥 CRITICAL FIX (Jan 29, 2026): ANTICIPATION BREAKOUT HAS PRIORITY
        # 
        # Si ANTICIPATION tiene señal COMPRESSION_BREAKOUT, el sistema debe:
        # 1. NO invalidar edges existentes por direction mismatch
        # 2. Permitir que el BREAKOUT edge reemplace cualquier edge anterior
        # 3. Usar la dirección de ANTICIPATION, no de micro_trend
        #
        # ANTICIPATION es el "leading indicator" - ve el setup ANTES del breakout.
        # micro_trend ve el ruido actual - irrelevante para compression breakout.
        # =====================================================================
        anticipation_signal = self._current_trade_tags.get('anticipation_signal')
        anticipation_direction = self._current_trade_tags.get('anticipation_direction')
        has_anticipation_breakout = (anticipation_signal == 'COMPRESSION_BREAKOUT' and anticipation_direction)
        
        # Si hay ANTICIPATION BREAKOUT pendiente, sincronizar dirección activa
        if has_anticipation_breakout:
            if self._active_direction != anticipation_direction:
                logger.info(
                    f"🎯 ANTICIPATION OVERRIDE: Active direction {self._active_direction}→{anticipation_direction} | "
                    f"COMPRESSION_BREAKOUT detected - using anticipation direction"
                )
                self._active_direction = anticipation_direction
                self.current_bias = anticipation_direction
                self._current_trade_tags['anticipation_direction_override'] = True
        
        # =====================================================================
        # 🔥 CRITICAL FIX (Jan 29, 2026): BREAKOUT EDGE DIRECTION OVERRIDE
        # 
        # PROBLEMA ANTERIOR:
        #   - ANTICIPATION detecta COMPRESSION_BREAKOUT LONG
        #   - Edge se arma con direction=LONG
        #   - Pero micro_trend/MTF dice SHORT (ruido)
        #   - Sistema INVALIDABA el edge → PERDÍA EL TRADE CORRECTO
        #   - O peor: ejecutaba SHORT cuando el edge era LONG
        #
        # SOLUCIÓN INSTITUCIONAL:
        #   - BREAKOUT edge tiene AUTORIDAD SOBERANA sobre dirección
        #   - Cuando hay BREAKOUT edge armado/activo:
        #     → active_direction = edge.direction (OVERRIDE)
        #     → NO invalidar el edge
        #   - Para TREND/RANGE edges: mantener lógica de invalidación (opcional)
        #   - Si hay ANTICIPATION BREAKOUT pendiente: NO invalidar nada
        # =====================================================================
        if self._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            edge_direction = None
            edge_type = None
            if self._edge_manager.current_bos:
                edge_direction = self._edge_manager.current_bos.direction
                edge_type = self._edge_manager.current_bos.edge_type
            
            if edge_direction and self._active_direction:
                if edge_direction != self._active_direction:
                    # =========================================================
                    # 🔥 BREAKOUT/DISPLACEMENT: EDGE TIENE AUTORIDAD - OVERRIDE DIRECTION
                    # =========================================================
                    if edge_type == EdgeType.BREAKOUT:
                        logger.info(
                            f"🎯 BREAKOUT DIRECTION OVERRIDE: Edge={edge_direction} | "
                            f"Ignoring micro={self._active_direction} | "
                            f"BREAKOUT edge has sovereign authority"
                        )
                        # OVERRIDE: usar dirección del edge, NO invalidar
                        self._active_direction = edge_direction
                        bias = edge_direction  # También actualizar bias
                        self._current_trade_tags['breakout_direction_override'] = True
                        self._current_trade_tags['original_micro_direction'] = self._active_direction
                        self._current_trade_tags['active_direction'] = edge_direction
                    # =========================================================
                    # 🔥 DISPLACEMENT: EDGE TIENE AUTORIDAD SOBERANA (Jan 30, 2026)
                    # El displacement ES el momentum institucional confirmado.
                    # NO invalidar por micro-fluctuaciones.
                    # =========================================================
                    elif edge_type == EdgeType.DISPLACEMENT:
                        logger.info(
                            f"🌊 DISPLACEMENT DIRECTION OVERRIDE: Edge={edge_direction} | "
                            f"Ignoring micro={self._active_direction} | "
                            f"DISPLACEMENT edge has sovereign authority (institutional momentum)"
                        )
                        # OVERRIDE: usar dirección del edge, NO invalidar
                        self._active_direction = edge_direction
                        bias = edge_direction
                        self._current_trade_tags['displacement_direction_override'] = True
                        self._current_trade_tags['original_micro_direction'] = self._active_direction
                        self._current_trade_tags['active_direction'] = edge_direction
                    # =========================================================
                    # 🔥 ANTICIPATION PENDING: No invalidar si hay breakout pendiente
                    # =========================================================
                    elif has_anticipation_breakout:
                        logger.info(
                            f"🎯 ANTICIPATION BREAKOUT PENDING: Skipping edge invalidation | "
                            f"Old edge will be replaced by BREAKOUT {anticipation_direction}"
                        )
                        # No invalidar - dejar que el nuevo BREAKOUT tome control
                    else:
                        # TREND/RANGE: invalidar si hay mismatch (comportamiento original)
                        logger.info(
                            f"🔄 EDGE INVALIDATED: Direction mismatch | "
                            f"Edge={edge_direction} vs Active={self._active_direction} | "
                            f"Market changed direction → resetting edge"
                        )
                        self._edge_manager._transition_to(
                            EdgeState.NO_EDGE,
                            f"Direction flip: {edge_direction}→{self._active_direction}",
                            market_time
                        )
                        self._current_trade_tags['edge_invalidated_by'] = 'direction_flip'
        
        # =====================================================================
        # 🔥 FIX (Jan 27, 2026): DETECCIÓN DE EDGE POR TIPO DE MERCADO
        # 
        # PROBLEMA ORIGINAL:
        #   - En RANGE (ADX<18), no hay BOS → NO_EDGE → NO TRADES
        #   - El sistema detectaba PROBING/EXTREMOS pero EdgeStateManager
        #     solo entendía TREND (BOS+pullback)
        #
        # SOLUCIÓN:
        #   - TREND (ADX≥18): BOS + Pullback (original)
        #   - RANGE (ADX<18): Extremos (85%/15%) o PROBING con alta confluencia
        #   - BREAKOUT: Compresión + ruptura (para ANTICIPATION)
        # =====================================================================
        
        # 2. Detectar edge según modo de trading
        if self._edge_manager.state == EdgeState.NO_EDGE:
            
            # =====================================================================
            # 🔥 FIX (Jan 28, 2026): BREAKOUT EDGE - Integrar con ANTICIPATION
            # 
            # Cuando ANTICIPATION detecta COMPRESSION_BREAKOUT, activar edge de breakout
            # Esto conecta el sistema de anticipación con el edge state manager
            # =====================================================================
            anticipation_signal = self._current_trade_tags.get('anticipation_signal')
            if anticipation_signal == 'COMPRESSION_BREAKOUT':
                anticipation_direction = self._current_trade_tags.get('anticipation_direction', 'LONG')
                anticipation_bb_pct = self._current_trade_tags.get('anticipation_bb_percentile', 50.0)
                anticipation_vol_ratio = self._current_trade_tags.get('anticipation_volume_ratio', 1.0)
                
                # Intentar armar edge de breakout
                breakout_armed = self._edge_manager.check_for_breakout_edge(
                    current_price=current_price,
                    bb_width_percentile=anticipation_bb_pct,
                    volume_ratio=anticipation_vol_ratio,
                    direction=anticipation_direction,
                    atr=atr_1h,
                    market_time=market_time
                )
                
                if breakout_armed:
                    self._current_trade_tags['breakout_edge_armed'] = True
                    self._current_trade_tags['edge_type'] = 'BREAKOUT'
                    logger.info(
                        f"🔥 BREAKOUT EDGE ARMED from ANTICIPATION | "
                        f"Direction: {anticipation_direction} | BB%: {anticipation_bb_pct:.0f}"
                    )
            
            if trading_mode == 'RANGE':
                # RANGE: Buscar edge en extremos CON SWEEP + REJECTION obligatorio
                percentile_60d = self._location_context.percentile_60d if self._location_context else 50.0
                
                # =====================================================================
                # 🔥 FIX (Jan 28, 2026): PROBING ≠ EDGE
                # 
                # PROBING es un TAG INFORMATIVO, NO activa edge automáticamente.
                # Solo indica que estamos cerca de VWAP/VPOC para referencia.
                #
                # Para entrar necesitamos:
                # - EXTREMOS (pct <=15% o >=85%) + sweep/rejection, O
                # - CONFIRMACIÓN adicional (BOS, delta aligned, etc.)
                #
                # 🔥 FIX (Jan 28, 2026): PROBING ELIMINADO EN RANGE
                # Ya no hay probing en RANGE - solo trades en extremos reales
                # Este código solo registra para observabilidad, no activa nada
                # =====================================================================
                
                # =====================================================================
                # 🔥 SWEEP + REJECTION: Obtener datos de últimas 2 velas 5m
                # Sin sweep+rejection NO hay edge, solo localización
                # =====================================================================
                curr_high, curr_low, curr_open, curr_close = 0.0, 0.0, 0.0, 0.0
                prev_high, prev_low = 0.0, 0.0
                
                if candles_5m and len(candles_5m) >= 2:
                    curr_candle = candles_5m[-1]
                    prev_candle = candles_5m[-2]
                    
                    curr_high = float(curr_candle.get('high', 0))
                    curr_low = float(curr_candle.get('low', 0))
                    curr_open = float(curr_candle.get('open', 0))
                    curr_close = float(curr_candle.get('close', 0))
                    prev_high = float(prev_candle.get('high', 0))
                    prev_low = float(prev_candle.get('low', 0))
                
                # =====================================================================
                # 🔥 COMPRESIÓN CHECK: Calcular BB width percentile
                # Si BB width <= 5% percentile → RANGE deshabilitado (pre-breakout)
                # =====================================================================
                bb_width_percentile = 50.0  # Default neutral
                try:
                    candles_1m = self.market_data.get_candles('1m')
                    if candles_1m and len(candles_1m) >= 50:
                        from src.shared.indicators import calculate_bollinger_bands
                        df_1m = pd.DataFrame(candles_1m)
                        closes_1m = df_1m['close'].to_numpy()
                        bb_upper, bb_middle, bb_lower = calculate_bollinger_bands(closes_1m, period=20, std_dev=2)
                        if len(bb_upper) >= 20:
                            bb_widths = bb_upper[-20:] - bb_lower[-20:]
                            current_bb_width = bb_upper[-1] - bb_lower[-1]
                            # Percentile: qué % de valores son MENORES que el actual
                            bb_width_percentile = (np.sum(bb_widths < current_bb_width) / len(bb_widths)) * 100
                except Exception as e:
                    logger.debug(f"BB width percentile calc failed: {e}")
                
                range_edge = self._edge_manager.check_for_range_edge(
                    current_price=current_price,
                    htf_bias=bias,
                    percentile_60d=percentile_60d,
                    atr=atr_1h,
                    market_time=market_time,
                    # Datos para sweep + rejection
                    curr_high=curr_high,
                    curr_low=curr_low,
                    curr_open=curr_open,
                    curr_close=curr_close,
                    prev_high=prev_high,
                    prev_low=prev_low,
                    # 🔥 BB width percentile para compresión check
                    bb_width_percentile=bb_width_percentile
                )
                
                if range_edge:
                    self._current_trade_tags['range_edge_activated'] = True
                    self._current_trade_tags['edge_type'] = 'RANGE'
                    self._current_trade_tags['sweep_rejection_confirmed'] = True
                    # El SL binario está en el BOSEvent.bos_level
                    if self._edge_manager.current_bos:
                        self._current_trade_tags['range_invalidation_level'] = self._edge_manager.current_bos.bos_level
            
            else:
                # TREND/TRANSITION: Buscar BOS clásico
                bos_detected = self._edge_manager.check_for_bos(
                    current_price=current_price,
                    htf_bias=bias,
                    swing_high=self._last_swing_high,
                    swing_low=self._last_swing_low,
                    prev_swing_high=self._prev_swing_high,
                    prev_swing_low=self._prev_swing_low,
                    atr=atr_1h,
                    market_time=market_time
                )
                if bos_detected:
                    self._current_trade_tags['bos_detected'] = True
                    self._current_trade_tags['edge_type'] = 'TREND'
        
        # 3. Check for pullback o breakout confirmation (si edge está armado)
        if self._edge_manager.state == EdgeState.EDGE_ARMED:
            current_bos = self._edge_manager.current_bos
            
            if current_bos and current_bos.edge_type == EdgeType.DISPLACEMENT:
                # =====================================================================
                # 🌊 DISPLACEMENT CONFIRMATION (Jan 30, 2026)
                # 
                # El displacement edge se confirma si:
                # 1. Hay pullback pequeño (15-50% del displacement) = entrada óptima
                # 2. O hay continuación (+10% más) = follow-through
                # =====================================================================
                current_displacement = 0.0
                if current_bos.direction == 'SHORT':
                    current_displacement = self._bias_high_since - current_price
                else:
                    current_displacement = current_price - self._bias_low_since
                
                displacement_confirmed = self._edge_manager.confirm_displacement_edge(
                    current_price=current_price,
                    displacement_direction=current_bos.direction,
                    initial_displacement=self._expansion_initial_displacement if hasattr(self, '_expansion_initial_displacement') else 0,
                    current_displacement=current_displacement,
                    atr=atr_1h,
                    market_time=market_time
                )
                
                if displacement_confirmed:
                    self._current_trade_tags['displacement_confirmed'] = True
                    logger.info(
                        f"🌊 DISPLACEMENT EDGE CONFIRMED: {current_bos.direction} | "
                        f"Current disp: ${current_displacement:.0f}"
                    )
            
            elif current_bos and current_bos.edge_type == EdgeType.BREAKOUT:
                # =====================================================================
                # 🔥 BREAKOUT CONFIRMATION (Jan 28, 2026): Criterios institucionales
                # 
                # No solo distancia ATR - necesitamos:
                # 1. Close FUERA de Bollinger Bands
                # 2. Dirección coincide con anticipation
                # 3. Expansión de BB comenzando
                # =====================================================================
                
                # Obtener datos de BB para confirmación institucional
                bb_upper, bb_lower, bb_expansion_ratio = 0.0, 0.0, 0.0
                close_price = current_price  # Default al precio actual
                
                try:
                    candles_1m = self.market_data.get_candles('1m')
                    if candles_1m and len(candles_1m) >= 50:
                        from src.shared.indicators import calculate_bollinger_bands
                        df_1m = pd.DataFrame(candles_1m)
                        closes_1m = df_1m['close'].to_numpy()
                        bb_upper_arr, bb_middle, bb_lower_arr = calculate_bollinger_bands(closes_1m, period=20, std_dev=2)
                        
                        if len(bb_upper_arr) >= 20:
                            bb_upper = float(bb_upper_arr[-1])
                            bb_lower = float(bb_lower_arr[-1])
                            close_price = float(closes_1m[-1])
                            
                            # Calcular ratio de expansión
                            bb_widths = bb_upper_arr[-20:] - bb_lower_arr[-20:]
                            avg_bb_width = float(np.mean(bb_widths[:-1]))  # Excluir actual
                            current_bb_width = bb_upper - bb_lower
                            bb_expansion_ratio = float(current_bb_width / avg_bb_width) if avg_bb_width > 0 else 1.0
                except Exception as e:
                    logger.debug(f"BB data for breakout confirmation failed: {e}")
                
                # Verificar dirección correcta primero
                direction_ok = (
                    (current_bos.direction == 'LONG' and current_price > current_bos.price) or
                    (current_bos.direction == 'SHORT' and current_price < current_bos.price)
                )
                
                if direction_ok:
                    breakout_confirmed = self._edge_manager.confirm_breakout(
                        current_price=current_price,
                        breakout_direction=current_bos.direction,
                        atr=atr_1h,
                        market_time=market_time,
                        bb_upper=bb_upper,
                        bb_lower=bb_lower,
                        close_price=close_price,
                        expansion_ratio=bb_expansion_ratio
                    )
                    if breakout_confirmed:
                        self._current_trade_tags['breakout_confirmed'] = True
                        self._current_trade_tags['bb_expansion_ratio'] = bb_expansion_ratio
            else:
                # TREND edge - check pullback
                pullback_valid = self._edge_manager.check_for_pullback(
                    current_price=current_price,
                    atr=atr_1h,
                    market_time=market_time
                )
                if pullback_valid:
                    self._current_trade_tags['pullback_validated'] = True
        
        # 4. Guardar estado del edge en tags para observabilidad
        edge_status = self._edge_manager.get_status()
        self._current_trade_tags['edge_state'] = edge_status['state']
        self._current_trade_tags['edge_can_trade'] = edge_status['can_trade']
        if edge_status.get('current_bos'):
            self._current_trade_tags['edge_bos_direction'] = edge_status['current_bos']['direction']
            self._current_trade_tags['edge_trades_remaining'] = edge_status['current_bos']['trades_remaining']
        
        # =====================================================================
        # 🔥 ARQUITECTURA CORRECTA (Jan 29, 2026): EDGE OWNS DIRECTION
        # 
        # REGLA FUNDAMENTAL:
        #   La dirección SOLO puede nacer en un sitio: ANTICIPATION / EDGE
        #   HTF/MTF/LTF NUNCA generan dirección - solo VALIDAN ejecución
        #
        # Si hay edge armado/activo:
        #   → bias = edge.direction (NO NEGOCIABLE)
        #   → Los filtros validan si PODEMOS EJECUTAR ese edge
        #   → Los filtros NO deciden dirección contraria
        # =====================================================================
        if self._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            edge_bos = self._edge_manager.current_bos
            if edge_bos and edge_bos.direction:
                edge_direction = edge_bos.direction
                
                # Si bias actual difiere del edge → OVERRIDE con edge direction
                if bias != edge_direction:
                    old_bias = bias
                    bias = edge_direction
                    self.current_bias = bias
                    self._active_direction = bias
                    
                    logger.info(
                        f"🎯 EDGE AUTHORITY: Direction {old_bias}→{edge_direction} | "
                        f"Edge type: {edge_bos.edge_type.value} | "
                        f"Edge owns direction, filters only validate"
                    )
                    self._current_trade_tags['edge_direction_override'] = True
                    self._current_trade_tags['original_bias'] = old_bias
        
        # 5. Si NO hay edge activo → skip evaluación (el core del cambio)
        # PERO: Permitir modo PROBING para diagnóstico (0.25x size)
        # 🔥 FIX (Jan 28, 2026): PROBE mode puede operar sin edge activo
        #    ProbeValidator valida rentabilidad mínima
        # 🔥 FIX (Jan 28): PROBE mode eliminado en RANGE
        # Solo TREND/TRANSITION/BREAKOUT pueden tener edge activo
        
        if not self._edge_manager.can_evaluate_entry():
            block_reason = self._edge_manager.get_block_reason()
            
            # Log cada 2 minutos
            if self._idle_eval_count % 120 == 1:
                logger.info(f"⏸️ {block_reason}")
                
                # Log expectancy stats si hay datos
                stats = self._edge_manager.stats
                if stats.total_bos_events > 0:
                    logger.info(
                        f"   📊 Edge Stats: {stats.total_bos_events} BOS events | "
                        f"Expectancy/BOS: {stats.get_expectancy_per_bos():.3f}R"
                    )
            
            # En estado EDGE_ARMED, no salir - el edge está esperando pullback
            # Solo bloquear si estamos en NO_EDGE o EXPIRED
            if self._edge_manager.state not in [EdgeState.EDGE_ARMED]:
                return
        
        # Edge activo - continuar con evaluación normal
        if self._edge_manager.can_trade():
            bos = self._edge_manager.current_bos
            if bos:
                logger.info(
                    f"🎯 EDGE ACTIVE: {bos.direction} | BOS level: {bos.bos_level:.2f} | "
                    f"Trades: {bos.trades_attempted}/{self._edge_manager.MAX_TRADES_PER_EDGE}"
                )
        
        # ===== BLOQUE 1.5: LOCATION FILTER (MACRO VETO) =====
        # Check if location in market structure allows this trade direction
        # This is a HARD VETO that prevents buying tops in RANGE regime
        
        volumes_1h = np.asarray(df_1h['volume'].to_numpy()) if 'volume' in df_1h.columns else np.ones(len(closes_1h))
        atr_1h = atr_1h_vals[-1] if len(atr_1h_vals) > 0 else current_price * 0.01
        
        adx_1h_vals, _ = calculate_adx(highs_1h, lows_1h, closes_1h, 14)
        adx_1h = adx_1h_vals[-1] if len(adx_1h_vals) > 0 else 20.0
        
        # Get current regime for location filter
        regime_for_location = self.regime_classifier.current_regime.value if self.regime_classifier.current_regime else "RANGE"
        
        # 🔥 FIX (Jan 23, 2026): HTF ADX classification can OVERRIDE regime to RANGE
        # If HTF ADX says RANGE/WEAK_TREND but EdgeRegimeClassifier says TREND, trust ADX
        # This fixes the bug where ADX 15.3 was treated as TREND (wrong stops, wrong logic)
        htf_regime_mode = self._htf_cache_regime_mode
        if htf_regime_mode in ('RANGE', 'WEAK_TREND', 'LOW_ADX'):
            if regime_for_location in ('REGIME_A', 'TREND', 'BREAKOUT'):
                logger.info(f"⚠️ HTF ADX override: classifier={regime_for_location} but ADX={self._htf_cache_adx:.1f} → forcing RANGE logic")
                regime_for_location = 'RANGE'
        
        # 🔥 FIX (Jan 22, 2026): Store regime for trailing stop to use SAME regime
        self._current_trade_regime = regime_for_location
        
        # 🔥 FIX (Jan 23, 2026): Store HTF regime mode in trade tags for analysis
        self._current_trade_tags['htf_regime_mode'] = htf_regime_mode
        self._current_trade_tags['htf_adx'] = self._htf_cache_adx
        
        # Calculate location context (cached by 1h bar close)
        # Ensure bias is set before location check
        if bias is None:
            logger.warning("HTF passed but no bias set - skipping")
            return
        
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
            # 🔥 FIX (Jan 22, 2026): Store location penalties for scoring
            self._current_trade_tags['location_penalty_long'] = self._location_context.location_penalty_long
            self._current_trade_tags['location_penalty_short'] = self._location_context.location_penalty_short
            self._current_trade_tags['location_size_cap_long'] = self._location_context.size_cap_long
            self._current_trade_tags['location_size_cap_short'] = self._location_context.size_cap_short
        
        # 🔥 FIX (Jan 22, 2026): In TREND regime, convert hard veto to penalty + continue
        # Only hard block in RANGE regime
        is_range_regime = regime_for_location in ['RANGE', 'CHOPPY_DEAD', 'MEAN_REVERSION']
        
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
                # 🔥 FIX (Jan 22, 2026): In TREND regime, DON'T hard block - use penalty and continue
                if not is_range_regime:
                    # TREND/TRANSITION: Apply penalty and continue (don't return)
                    location_penalty = (
                        self._location_context.location_penalty_long if bias == 'LONG' 
                        else self._location_context.location_penalty_short
                    ) if self._location_context else -1.0
                    self._current_trade_tags['location_penalty_applied'] = location_penalty
                    logger.info(f"📍 LOCATION PENALTY (TREND): {location_reason} → penalty {location_penalty:.1f} (continuing)")
                    # Don't return - continue with penalty applied in scoring
                else:
                    # RANGE regime: Hard block only for extreme cases
                    # Log location veto (once per bias change)
                    if self._last_location_state is None or self._last_location_state != f'vetoed_{bias}':
                        logger.warning(f"📍 LOCATION VETO (RANGE): {location_reason}")
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
            
            # 🔥 FIX (Jan 22, 2026): Apply location penalty even when allowed
            # In RANGE regime with extreme positions, penalty is set but no hard block
            if self._location_context:
                location_penalty = (
                    self._location_context.location_penalty_long if bias == 'LONG' 
                    else self._location_context.location_penalty_short
                )
                if location_penalty < 0:
                    self._current_trade_tags['location_penalty_applied'] = location_penalty
                    logger.debug(f"📍 Location penalty (allowed): {location_penalty:.1f}")
        
        # =========================================================================
        # NEW: BIAS STRENGTH & TREND EXPANSION DETECTION (Jan 22, 2026)
        # Detect market state for asymmetric direction rules
        # =========================================================================
        
        # Get 5m data for expansion detection
        candles_5m_pre = self.market_data.get_candles('5m')
        df_5m_pre = pd.DataFrame(candles_5m_pre) if candles_5m_pre and len(candles_5m_pre) >= 30 else None
        candles_1m_pre = self.market_data.get_candles('1m')
        df_1m_pre = pd.DataFrame(candles_1m_pre) if candles_1m_pre and len(candles_1m_pre) >= 20 else None
        
        # 1. Detect Bias Strength (uses extracted detector)
        if df_5m_pre is not None and len(df_5m_pre) >= 20:
            bias_result = self._bias_strength_detector.detect(df_1h, df_5m_pre, current_price)
            self._current_bias_strength = bias_result.strength
        else:
            self._current_bias_strength = BiasStrength.NEUTRAL
        
        # 🔥 FIX: Ensure _current_bias_strength is not None before accessing .value
        if self._current_bias_strength is None:
            self._current_bias_strength = BiasStrength.NEUTRAL
        self._current_trade_tags['bias_strength'] = self._current_bias_strength.value
        
        # 2. Get countertrend rules
        ct_rules = BiasStrengthConfig.get_countertrend_rules(self._current_bias_strength, bias)
        is_countertrend = ct_rules['is_countertrend']
        
        self._current_trade_tags['is_countertrend'] = is_countertrend
        self._current_trade_tags['ct_size_mult'] = ct_rules['size_mult']
        self._current_trade_tags['ct_max_rr'] = ct_rules['max_rr']
        
        if is_countertrend:
            bias_value = self._current_bias_strength.value if self._current_bias_strength else 'NEUTRAL'
            logger.info(f"⚠️ COUNTERTREND TRADE: {bias} against {bias_value} → size={ct_rules['size_mult']:.0%}, max_rr={ct_rules['max_rr']:.1f}")
            
            # Check countertrend limits
            if self._countertrend_losses_session >= BiasStrengthConfig.COUNTERTREND_MAX_PER_SESSION:
                logger.warning(f"⛔ CT SESSION LIMIT: {self._countertrend_losses_session} CT losses this session")
                self._current_trade_tags['skip_reason'] = 'ct_session_limit'
                return
            
            # Check cooldown after CT loss
            if self._last_countertrend_loss_time is not None:
                cooldown_end = self._last_countertrend_loss_time + timedelta(minutes=ct_rules['cooldown_on_loss'] * 5)
                if market_time < cooldown_end:
                    remaining = (cooldown_end - market_time).total_seconds() / 60
                    logger.info(f"⏳ CT COOLDOWN: {remaining:.0f}min remaining after CT loss")
                    self._current_trade_tags['skip_reason'] = 'ct_cooldown'
                    return
        
        # 3. Detect Trend Expansion (uses extracted detector)
        if df_5m_pre is not None and df_1m_pre is not None:
            atr_5m_pre = calculate_atr(df_5m_pre['high'].to_numpy(), df_5m_pre['low'].to_numpy(), df_5m_pre['close'].to_numpy(), 14)
            atr_5m_val = atr_5m_pre[-1] if len(atr_5m_pre) > 0 else current_price * 0.005
            
            expansion_result = self._trend_expansion_detector.detect(
                df_5m_pre, df_1m_pre, current_price, atr_5m_val
            )
            expansion_detected = expansion_result.is_expanding
            expansion_criteria = expansion_result.criteria_met
            
            if expansion_detected and not self._trend_expansion_active:
                self._trend_expansion_active = True
                self._trend_expansion_start_time = market_time
                # Set anti-fade period
                anti_fade_minutes = TrendExpansionConfig.ANTI_FADE_CANDLES * 5
                self._anti_fade_until = market_time + timedelta(minutes=anti_fade_minutes)
                logger.info(f"🚀 TREND EXPANSION MODE ACTIVATED - anti-fade for {anti_fade_minutes}min")
            elif not expansion_detected and self._trend_expansion_active:
                # Check if expansion ended
                if self._trend_expansion_start_time and (market_time - self._trend_expansion_start_time).total_seconds() > 1800:  # 30min min duration
                    self._trend_expansion_active = False
                    self._trend_expansion_start_time = None
                    logger.info("📉 TREND EXPANSION MODE ENDED")
            
            self._current_trade_tags['trend_expansion'] = self._trend_expansion_active
            self._current_trade_tags['expansion_criteria'] = expansion_criteria
        
        # 4. Apply anti-fade rule (no counter-trend in expansion)
        if self._anti_fade_until and market_time < self._anti_fade_until:
            if is_countertrend:
                remaining = (self._anti_fade_until - market_time).total_seconds() / 60
                logger.info(f"⛔ ANTI-FADE: No countertrend during expansion ({remaining:.0f}min remaining)")
                self._current_trade_tags['skip_reason'] = 'anti_fade_expansion'
                return
        
        # 5. Short Obstinacy Check (for shorts only - uses extracted calculator)
        short_obstinacy_penalty = 0.0
        if bias == 'SHORT' and df_5m_pre is not None:
            atr_5m_pre = calculate_atr(df_5m_pre['high'].to_numpy(), df_5m_pre['low'].to_numpy(), df_5m_pre['close'].to_numpy(), 14)
            atr_5m_val = atr_5m_pre[-1] if len(atr_5m_pre) > 0 else current_price * 0.005
            
            short_obstinacy_penalty, bullish_signals, obstinacy_reason = self._short_obstinacy_calculator.calculate(
                df_5m_pre, current_price, atr_5m_val
            )
            
            self._current_trade_tags['short_obstinacy_penalty'] = short_obstinacy_penalty
            self._current_trade_tags['bullish_signals'] = bullish_signals
            self._current_trade_tags['obstinacy_reason'] = obstinacy_reason
            
            # Check short cooldown
            if self._short_cooldown_until and market_time < self._short_cooldown_until:
                remaining = (self._short_cooldown_until - market_time).total_seconds() / 60
                logger.info(f"⏳ SHORT OBSTINACY COOLDOWN: {remaining:.0f}min remaining")
                self._current_trade_tags['skip_reason'] = 'short_obstinacy_cooldown'
                return
        
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
        
        # Initialize to None for type safety
        df_5m_micro: pd.DataFrame | None = None
        df_1m_micro: pd.DataFrame | None = None
        
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
                
                # =========================================================================
                # 🔥 FIX (Jan 22, 2026): REGIME-AWARE MICRO TREND ADJUSTMENT
                # In RANGE regime: micro trends are ephemeral, reduce their weight
                # This prevents over-shorting/longing in the middle of ranges
                # =========================================================================
                regime_for_micro = self._current_trade_regime
                is_range_for_micro = regime_for_micro in ['RANGE', 'CHOPPY_DEAD', 'MEAN_REVERSION', 'TREND_EXHAUSTION']
                
                if is_range_for_micro and self._micro_trend_state.score_adjustment != 0:
                    original_adj = self._micro_trend_state.score_adjustment
                    # In RANGE: cap bonus at 0.5, cap penalty at -0.3
                    if original_adj > 0:
                        self._micro_trend_state.score_adjustment = min(0.5, original_adj)
                    else:
                        self._micro_trend_state.score_adjustment = max(-0.3, original_adj)
                    
                    if original_adj != self._micro_trend_state.score_adjustment:
                        logger.info(
                            f"📊 MICRO ADJ CAPPED (RANGE regime): {original_adj:+.1f} → "
                            f"{self._micro_trend_state.score_adjustment:+.1f}"
                        )
                        self._current_trade_tags['micro_score_adj'] = self._micro_trend_state.score_adjustment
                        self._current_trade_tags['micro_regime_capped'] = True
            
            if not micro_allowed:
                # =====================================================================
                # MICRO TREND ADX CONDITIONING (Jan 16, 2026)
                # Only apply hard block if HTF trend is WEAK (low ADX)
                # In STRONG trends (ADX >= 30), micro-corrections are normal - allow entry
                # In WEAK trends (ADX < 20), micro-trend signals are more meaningful - block
                #
                # 🔥 FIX (Jan 22, 2026): Penalty caps based on ADX
                # ADX > 30: penalty = 0 (strong trend, pullbacks are opportunities)
                # ADX 15-25: penalty max = -0.5 (transitional, be cautious but allow)
                # ADX < 15: keep current penalty (weak/ranging, micro trend is significant)
                # =====================================================================
                adx_override = False
                
                if adx_1h >= MicroTrendADXConfig.STRONG_TREND_ADX:
                    # Strong HTF trend - override micro block, NO penalty
                    adx_override = True
                    self._current_trade_tags['micro_adx_override'] = True
                    self._current_trade_tags['micro_htf_adx'] = adx_1h
                    logger.info(f"⚡ MICRO TREND override: HTF ADX={adx_1h:.1f} >= {MicroTrendADXConfig.STRONG_TREND_ADX} (strong trend - NO penalty)")
                    # Zero penalty in strong trends
                    if self._micro_trend_state:
                        self._micro_trend_state.score_adjustment = 0.0
                elif adx_1h >= 15:  # 15-30 range
                    # Mid-range ADX: Apply capped penalty
                    adx_override = True
                    self._current_trade_tags['micro_adx_override'] = True
                    self._current_trade_tags['micro_htf_adx'] = adx_1h
                    
                    # Progressive penalty: 15 ADX = -0.5, 25 ADX = -0.25, 30 ADX = 0
                    penalty_factor = (30 - adx_1h) / 30  # 0 at ADX=30, 0.5 at ADX=15
                    capped_penalty = max(-0.5, -penalty_factor)
                    
                    if self._micro_trend_state:
                        self._micro_trend_state.score_adjustment = capped_penalty
                    
                    logger.info(f"⚠️ MICRO TREND: HTF ADX={adx_1h:.1f} (mid-range) → capped penalty {capped_penalty:.2f}")
                else:
                    # Weak HTF trend (ADX < 15) - micro signals are very significant
                    logger.info(f"⛔ MICRO TREND: HTF ADX={adx_1h:.1f} < 15 (weak trend, full block)")
                
                if not adx_override:
                    # Log micro trend veto (once per state change)
                    if self._last_micro_state is None or self._last_micro_state != f'vetoed_{bias}':
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
        
        # =========================================================================
        # 🔥 BIAS INVERSION (Jan 27, 2026)
        # PROBLEM: HTF bias (EMA50_1h) is slow to react to intraday trend changes.
        # When price drops $1000, EMA50 barely moves, so system keeps trying LONG.
        # SOLUTION: If micro-trend is STRONG and OPPOSES HTF bias, INVERT the bias.
        # 
        # This allows the system to trade the actual trend, not a lagging indicator.
        # 
        # ⚠️ CRITICAL FIX (Jan 29, 2026): NEVER invert when BREAKOUT EDGE is armed!
        # The EDGE has sovereign authority over direction. ANTICIPATION detected
        # a specific pattern that says "this direction" - micro_trend MUST NOT override.
        # =========================================================================
        MICRO_STRENGTH_FOR_INVERSION = 0.65  # Need 65%+ micro-trend strength to invert
        original_bias = bias
        bias_inverted = False
        
        # 🔥 CHECK: Is there an armed BREAKOUT/DISPLACEMENT edge? If so, SKIP bias inversion entirely
        has_sovereign_edge_armed = False
        sovereign_edge_type = None
        if self._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            if self._edge_manager.current_bos:
                if self._edge_manager.current_bos.edge_type in [EdgeType.BREAKOUT, EdgeType.DISPLACEMENT]:
                    has_sovereign_edge_armed = True
                    sovereign_edge_type = self._edge_manager.current_bos.edge_type
                    sovereign_edge_direction = self._edge_manager.current_bos.direction
                    logger.debug(
                        f"🛡️ BIAS INVERSION BLOCKED: {sovereign_edge_type.value} edge armed ({sovereign_edge_direction}) - "
                        f"edge has sovereign authority over direction"
                    )
        
        if has_sovereign_edge_armed:
            # BREAKOUT/DISPLACEMENT edge armed - DO NOT allow micro_trend to change direction
            edge_dir = self._edge_manager.current_bos.direction if self._edge_manager.current_bos else bias
            logger.info(f"🛡️ MICRO_TREND BYPASS: {sovereign_edge_type.value} EDGE {edge_dir} has authority")
        # 🔥 FIX (Jan 29, 2026 - AUDIT BUG 8): Add explicit null check before accessing .micro_trend
        elif self._micro_trend_state is not None and hasattr(self._micro_trend_state, 'strength') and self._micro_trend_state.strength >= MICRO_STRENGTH_FOR_INVERSION:
            micro_trend = self._micro_trend_state.micro_trend
            
            # HTF says LONG but micro is BEARISH → invert to SHORT
            if bias == 'LONG' and micro_trend == 'BEARISH':
                bias = 'SHORT'
                bias_inverted = True
                logger.info(
                    f"🔄 BIAS INVERTED: HTF={original_bias} but micro={micro_trend} "
                    f"(strength={self._micro_trend_state.strength:.0%}) → trading {bias}"
                )
                self._current_trade_tags['bias_inverted'] = True
                self._current_trade_tags['bias_inversion_reason'] = f'micro_{micro_trend.lower()}'
                self._current_trade_tags['original_htf_bias'] = original_bias
                # Reset micro-trend penalty since we're now aligned
                self._micro_trend_state.score_adjustment = 0.0
            
            # HTF says SHORT but micro is BULLISH → invert to LONG
            elif bias == 'SHORT' and micro_trend == 'BULLISH':
                bias = 'LONG'
                bias_inverted = True
                logger.info(
                    f"🔄 BIAS INVERTED: HTF={original_bias} but micro={micro_trend} "
                    f"(strength={self._micro_trend_state.strength:.0%}) → trading {bias}"
                )
                self._current_trade_tags['bias_inverted'] = True
                self._current_trade_tags['bias_inversion_reason'] = f'micro_{micro_trend.lower()}'
                self._current_trade_tags['original_htf_bias'] = original_bias
                # Reset micro-trend penalty since we're now aligned
                self._micro_trend_state.score_adjustment = 0.0
        
        # Update current_bias if inverted
        if bias_inverted:
            self.current_bias = bias
        
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
        
        if df_5m_micro is not None and len(df_5m_micro) >= 50:
            closes_5m = df_5m_micro['close'].to_numpy()
            ema20_5m_val = calculate_ema(closes_5m, 20)[-1]
            ema50_5m_val = calculate_ema(closes_5m, 50)[-1]
            
            if not np.isnan(ema20_5m_val) and not np.isnan(ema50_5m_val) and ema20_5m_val > 0:
                if bias == 'LONG':
                    # For LONG: Block if price is significantly BELOW EMAs
                    distance_to_ema20 = (current_price - ema20_5m_val) / ema20_5m_val
                    distance_to_ema50 = (current_price - ema50_5m_val) / ema50_5m_val
                    
                    # HARD BLOCK: Price >1% below EMA20 AND below EMA50
                    # 🔥 BYPASS: DISPLACEMENT edge has sovereign authority (Jan 30, 2026)
                    has_displacement_edge = (
                        self._edge_manager.current_bos and 
                        self._edge_manager.current_bos.edge_type == EdgeType.DISPLACEMENT and
                        self._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]
                    )
                    if distance_to_ema20 < -EMA_DISTANCE_BLOCK_PCT and distance_to_ema50 < 0:
                        if has_displacement_edge or self._expansion_mode:
                            logger.info(
                                f"🌊 EMA DISTANCE BYPASS: DISPLACEMENT/EXPANSION active - "
                                f"price {abs(distance_to_ema20)*100:.1f}% below EMA20 but momentum overrides"
                            )
                            self._current_trade_tags['ema_distance_bypassed'] = True
                        else:
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
                        logger.debug(
                            f"[WARN] EMA DISTANCE: LONG {abs(distance_to_ema20)*100:.1f}% below EMA20"
                        )
                        self._current_trade_tags['ema_distance_warning'] = True
                        self._current_trade_tags['ema20_distance_pct'] = distance_to_ema20 * 100
                
                elif bias == 'SHORT':
                    # For SHORT: Block if price is significantly ABOVE EMAs
                    distance_to_ema20 = (current_price - ema20_5m_val) / ema20_5m_val
                    distance_to_ema50 = (current_price - ema50_5m_val) / ema50_5m_val
                    
                    # HARD BLOCK: Price >1% above EMA20 AND above EMA50
                    # 🔥 BYPASS: DISPLACEMENT edge has sovereign authority (Jan 30, 2026)
                    has_displacement_edge_short = (
                        self._edge_manager.current_bos and 
                        self._edge_manager.current_bos.edge_type == EdgeType.DISPLACEMENT and
                        self._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]
                    )
                    if distance_to_ema20 > EMA_DISTANCE_BLOCK_PCT and distance_to_ema50 > 0:
                        if has_displacement_edge_short or self._expansion_mode:
                            logger.info(
                                f"🌊 EMA DISTANCE BYPASS: DISPLACEMENT/EXPANSION active - "
                                f"price {distance_to_ema20*100:.1f}% above EMA20 but momentum overrides"
                            )
                            self._current_trade_tags['ema_distance_bypassed'] = True
                        else:
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
        df_15m_proxy = df_5m_micro
        
        # Calculate spread from order book if available
        spread = None
        if hasattr(self, 'order_book') and self.order_book:
            bid_price, ask_price, spread_pct = self.order_book.get_spread()
            if bid_price and ask_price:
                # Update spread filter with bid/ask for rolling window analysis
                self.spread_filter.update_spread(bid_price, ask_price, current_price)
                spread = spread_pct  # Use the percentage for other calculations
                # 🔥 FIX (Jan 28, 2026): Store spread for SL updates in exit_actions
                if spread_pct:
                    self._last_spread_bps = spread_pct * 100  # Convert to basis points
        
        filter_decision = self.filter_pipeline.evaluate(
            df_1m=df_1m_micro,  # pyright: ignore[reportArgumentType]
            df_5m=df_5m_micro,  # pyright: ignore[reportArgumentType]
            df_15m=df_15m_proxy,  # pyright: ignore[reportArgumentType]
            df_1h=df_1h,
            current_price=current_price,
            bias=bias,
            atr=atr_1m,
            spread=spread,
            market_time=market_time,
            displacement_bypass=self._displacement_bypass  # 🔥 FIX (Jan 29): Pass displacement state
        )
        
        # Store filter pipeline tags
        if filter_decision.tags:
            for key, value in filter_decision.tags.items():
                self._current_trade_tags[f'fp_{key}'] = value
        
        if not filter_decision.allow:
            # Log which filter blocked
            blocker = filter_decision.tags.get('blocked_by', 'unknown')
            if self._last_fp_block is None or self._last_fp_block != f'{blocker}_{bias}':
                logger.warning(f"[BLOCK] FILTER [{blocker}]: {filter_decision.reasons[0] if filter_decision.reasons else 'veto'}")
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
        
        # =====================================================================
        # PUNTO 12: DELTA VOLUME CONFIRMATION (Jan 22, 2026)
        # Check if buy/sell volume delta confirms the bias direction
        # LONG: require positive delta (more buying than selling)
        # SHORT: require negative delta (more selling than buying)
        # This is a SCORE BONUS, not a hard gate
        # =====================================================================
        delta_score_bonus = 0
        try:
            if 'volume' in df_1m.columns and len(df_1m) >= 5:
                # Calculate volume delta using candle direction
                # Green candle = buy volume, Red candle = sell volume
                df_1m_recent = df_1m.tail(5).copy()
                df_1m_recent['is_green'] = df_1m_recent['close'] >= df_1m_recent['open']
                df_1m_recent['signed_vol'] = np.where(
                    df_1m_recent['is_green'],
                    df_1m_recent['volume'],
                    -df_1m_recent['volume']
                )
                delta = df_1m_recent['signed_vol'].sum()
                avg_vol = df_1m['volume'].tail(20).mean()
                
                # Calculate delta as multiple of average volume
                delta_ratio = delta / avg_vol if avg_vol > 0 else 0
                
                self._current_trade_tags['volume_delta'] = delta
                self._current_trade_tags['volume_delta_ratio'] = delta_ratio
                
                # Score bonus for aligned delta
                if bias == 'LONG' and delta_ratio > 0.5:
                    # Positive delta = buying pressure confirms LONG
                    delta_score_bonus = 0.5 if delta_ratio > 1.0 else 0.25
                    logger.debug(f"📊 DELTA ALIGNED: +{delta_ratio:.2f}x avg vol supports LONG (+{delta_score_bonus} score)")
                elif bias == 'SHORT' and delta_ratio < -0.5:
                    # Negative delta = selling pressure confirms SHORT
                    delta_score_bonus = 0.5 if delta_ratio < -1.0 else 0.25
                    logger.debug(f"📊 DELTA ALIGNED: {delta_ratio:.2f}x avg vol supports SHORT (+{delta_score_bonus} score)")
                elif bias == 'LONG' and delta_ratio < -0.5:
                    # Negative delta contradicts LONG - PENALTY in RANGE mode
                    self._current_trade_tags['delta_contradicts'] = True
                    # 🔥 FIX Jan 28: Delta contradict must penalize in RANGE (mean reversion depends on flow)
                    current_mode = self._current_trade_tags.get('trading_mode', '')
                    if current_mode == 'RANGE':
                        delta_score_bonus = -1.0  # Penalty in RANGE
                        logger.warning(f"⚠️ DELTA CONTRADICT RANGE: {delta_ratio:.2f}x vol AGAINST LONG → -1 score penalty")
                    else:
                        logger.info(f"⚠️ DELTA CONTRADICT: {delta_ratio:.2f}x avg vol AGAINST LONG bias")
                elif bias == 'SHORT' and delta_ratio > 0.5:
                    # Positive delta contradicts SHORT - PENALTY in RANGE mode
                    self._current_trade_tags['delta_contradicts'] = True
                    # 🔥 FIX Jan 28: Delta contradict must penalize in RANGE
                    current_mode = self._current_trade_tags.get('trading_mode', '')
                    if current_mode == 'RANGE':
                        delta_score_bonus = -1.0  # Penalty in RANGE
                        logger.warning(f"⚠️ DELTA CONTRADICT RANGE: +{delta_ratio:.2f}x vol AGAINST SHORT → -1 score penalty")
                    else:
                        logger.info(f"⚠️ DELTA CONTRADICT: +{delta_ratio:.2f}x avg vol AGAINST SHORT bias")
        except Exception as e:
            logger.debug(f"Delta volume calculation error: {e}")
        
        self._current_trade_tags['delta_score_bonus'] = delta_score_bonus
        
        # =====================================================================
        # 🔥 CRITICAL FIX (Jan 29, 2026): FINAL DIRECTION VALIDATION
        # 
        # ANTES DE EJECUTAR: Verificar que la dirección del trade coincide
        # con la dirección del edge (si hay edge armado/activo).
        # 
        # Esto previene el bug donde ANTICIPATION detecta LONG pero el
        # sistema ejecutaba SHORT basándose en otros filtros.
        # =====================================================================
        edge_direction_final = None
        edge_type_final = None
        if self._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            if self._edge_manager.current_bos:
                edge_direction_final = self._edge_manager.current_bos.direction
                edge_type_final = self._edge_manager.current_bos.edge_type
        
        # Si hay BREAKOUT edge, FORZAR su dirección (autoridad soberana)
        if edge_type_final == EdgeType.BREAKOUT and edge_direction_final:
            if bias != edge_direction_final:
                logger.warning(
                    f"⚠️ DIRECTION CORRECTION: bias={bias} → {edge_direction_final} | "
                    f"BREAKOUT edge has sovereign authority"
                )
                bias = edge_direction_final
                self.current_bias = bias
                self._active_direction = bias
                self._current_trade_tags['direction_corrected_by_breakout'] = True
        
        # ===== ALL FILTERS PASSED - PRE-CHECK EDGE CONTEXT =====
        # Note: LTF is "score-based" not "gate-based" so we show actual confirmations
        ltf_confs = self._current_trade_tags.get('ltf_confirmations', '?')
        
        # Quick edge context check BEFORE showing "SETUP COMPLETE"
        # This prevents confusing logs where setup appears complete but is then blocked
        if self._edge_context and not self._edge_context.is_valid():
            logger.info(f"🎯 FILTERS PASSED - {bias} | HTF✓ MTF✓ LTF({ltf_confs}/3) | ⚠️ Edge context pending validation")
        else:
            # 🔥 FIX: Log edge info if present
            edge_info = ""
            if edge_type_final and edge_direction_final:
                edge_info = f" | Edge: {edge_type_final.value} {edge_direction_final}"
            logger.info(f"🎯 SETUP COMPLETE - {bias} | HTF✓ MTF✓ LTF({ltf_confs}/3){edge_info}")
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
            from src.config.settings import get_settings
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
        position = self.risk_engine.position if hasattr(self.risk_engine, 'position') else None
        if self.risk_engine.has_open_position() and position is not None and hasattr(position, 'integrity_errors'):
            has_integrity_errors = len(position.integrity_errors) > 0
        
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
            if self._last_pullback_block_log is None or \
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
        """Calculate ATR from 1m data - delegates to indicator_helpers"""
        result = _calc_atr_1m_pure(df_1m)
        return result if result > 0 else 100.0

    async def _execute_entry(self, current_price: float, atr_1m: float, market_time: datetime):
        """
        Execute entry with strict safety checks.
        
        🔧 REFACTORED (Jan 28, 2026): Now delegates to entry_orchestrator.
        This reduces ~1940 lines of inline code to ~30 lines by using the orchestrated flow.
        
        The orchestrator handles:
        1. All pre-execution guards (concurrency, edge, location, ADX, funding, spread)
        2. Score calculation with all adjustments (spread, delta, obstinacy, location, anticipation)
        3. Entry quality gate
        4. Chase validation
        5. SL/TP calculation with regime adjustments
        6. RR validation
        7. Position sizing with 13+ multipliers
        8. Cost validation
        9. Order execution and state transitions
        """
        try:
            # Delegate to orchestrator
            result = await orchestrate_entry(self, current_price, atr_1m, market_time)
            
            if result.success:
                logger.info(f"✅ Entry orchestrated successfully: {result.order_id}")
                
                # Metrics
                from prometheus_client import Counter
                try:
                    signals_counter = Counter('signals_generated_total', 'Total signals generated', ['symbol', 'direction'])
                    signals_counter.labels(symbol=self.symbol, direction=self.current_bias).inc()
                except Exception as e:
                    logger.debug(f"Prometheus metric update error: {e}")
            else:
                if result.reason:
                    logger.debug(f"📊 Entry not executed: {result.reason}")
                    
        except Exception as e:
            logger.error(f"Entry orchestration error: {e}", exc_info=True)
            self._reset_trade_state("Entry orchestration exception")
            self.state_machine.start_cooldown(5)
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
                        
                        # 🔥 FIX (Jan 21, 2026): Update position_store in backtest WAIT_FILL
                        try:
                            fill_side = 'buy' if entry_data['bias'].upper() == 'LONG' else 'sell'
                            await self._position_store.record_fill(
                                order_id=self.pending_order_id or 'bt_order',
                                symbol=self.symbol,
                                side=fill_side,
                                size=fill_size,
                                price=fill_price,
                                fee=0.0,
                                fill_type='MAKER',
                                fill_id=f"{self.pending_order_id}_fill",
                            )
                        except Exception as ps_err:
                            logger.warning(f"⚠️ Could not update position_store (backtest WAIT_FILL): {ps_err}")
                        
                        self.risk_engine.create_position(
                            symbol=self.symbol,
                            direction=entry_data['bias'],
                            entry_price=fill_price,
                            position_size=fill_size,
                            stop_loss=entry_data['stop_loss'],
                            atr_1m=entry_data['atr_1m'],
                            market_time=market_time,
                            trade_type=entry_data.get('trade_type', TradeType.LONG_MAIN if entry_data['bias'] == 'LONG' else TradeType.SHORT_STRUCTURAL),
                            regime_mode=self._current_trade_regime  # 🔥 FIX (Jan 22): Pass regime to trailing
                        )
                        
                        # 🔥 NEW: Start exhaustion tracking for this trade
                        self.exhaustion_detector.start_tracking(market_time)
                        self._exhaustion_tightened = False
                        
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
            # PASO 2 — MAKER ONLY REAL (SIN EXCEPCIONES)
            # No LIMIT→MARKET fallback. If it doesn't fill, we cancel on timeout.
            
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
                order_type = 'market'  # default
                was_converted = False
                
                if hasattr(self, '_last_entry_data') and self._last_entry_data:
                    entry_ts = self._last_entry_data.get('tags', {}).get('entry_timestamp_ms', 0)
                    order_type = self._last_entry_data.get('order_type', 'LIMIT').lower()
                    # Check if this was a LIMIT→MARKET conversion (originally LIMIT but now MARKET)
                    original_order_type = self._last_entry_data.get('original_order_type', order_type)
                    was_converted = (original_order_type.lower() == 'limit' and order_type == 'market')
                    
                    if entry_ts:
                        fill_ts = int(market_time.timestamp() * 1000)
                        fill_latency_ms = fill_ts - entry_ts
                        self._last_entry_data['tags']['order_fill_latency_ms'] = fill_latency_ms
                        if fill_latency_ms > 5000:  # > 5 seconds
                            logger.info(f"🏷️ TAG: Order→fill latency: {fill_latency_ms}ms ({order_type}, converted={was_converted})")
                
                # =====================================================================
                # 🔥 FIX (Jan 23, 2026): Setup TTL - si fill_latency > 20s el contexto LTF
                # ya no es fiable. Cancelar y esperar otro setup.
                # Aplica SOLO a market orders puros, no a conversiones (damage control)
                # =====================================================================
                SETUP_TTL_MS = 20000  # 20 segundos máximo para un setup válido
                
                if order_type == 'market' and not was_converted and fill_latency_ms > SETUP_TTL_MS:
                    logger.warning(f"⚠️ SETUP TTL EXPIRED: {fill_latency_ms}ms > {SETUP_TTL_MS}ms - LTF context stale")
                    logger.warning("📉 Closing stale entry - waiting for fresh setup")
                    
                    # Close the position immediately
                    try:
                        await self.execution.close_position(self.symbol)
                        self._reset_trade_state("Setup TTL expired")
                        self.state_machine.transition_to(BotState.IDLE, "Setup TTL expired")
                        
                        # Record as health event (optional, informational)
                        if self.health_monitor:
                            self.health_monitor.record_order_filled(
                                latency_ms=fill_latency_ms, order_type=order_type, was_converted=False
                            )
                        return  # Exit without creating position
                    except Exception as close_err:
                        logger.error(f"❌ Could not close stale entry: {close_err}")
                        # Continue anyway - better to have tracking than phantom position
                
                # 📊 CAPA 5: Record fill in health monitor
                # 🔥 FIX (Jan 23, 2026): Pass order type to distinguish LIMIT wait time vs MARKET latency
                if self.health_monitor:
                    self.health_monitor.record_order_filled(
                        latency_ms=fill_latency_ms, 
                        order_type=order_type,
                        was_converted=was_converted
                    )
                
                # CRITICAL: Verificar que hay contracts en exchange
                await asyncio.sleep(0.5)  # Dar tiempo a que exchange actualice
                exchange_has_position = await self.execution.has_open_position(self.symbol)
                
                if not exchange_has_position:
                    logger.error("⚠️ Order filled but NO position on exchange - possible partial fill or error")
                    self._reset_trade_state("Fill reported but no position")
                    self.state_machine.transition_to(BotState.IDLE, "No position after fill")
                    return
                
                logger.info("✅ Position confirmed on exchange - creating local tracking")
                
                # Get position data for validation and later use
                position_data: dict | None = None
                
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
                    
                    # =========================================================================
                    # 🔥 FIX (Jan 21, 2026): Update position_store when limit order fills
                    # This was missing, causing position_store vs risk_engine desync
                    # leading to phantom positions and "Position flipped" errors
                    # 
                    # 🔥 FIX (Jan 22, 2026): SKIP if market order already recorded fill!
                    # When LIMIT→MARKET conversion happens, execution.py already calls
                    # record_fill, so we must NOT call it again here.
                    # Check if position_store already has this position to avoid double-count.
                    # =========================================================================
                    try:
                        fill_side = 'buy' if entry_data['bias'].upper() == 'LONG' else 'sell'
                        
                        # Check if position already exists (market order already recorded)
                        existing_pos = self._position_store.get_position(self.symbol or '')
                        if existing_pos and abs(existing_pos.size - actual_size) < 0.00001:
                            logger.info(f"✅ position_store already updated (market order path): {fill_side.upper()} {actual_size:.6f} @ {existing_pos.entry_price:.2f}")
                        else:
                            await self._position_store.record_fill(
                                order_id=self.pending_order_id or 'unknown',
                                symbol=self.symbol,
                                side=fill_side,
                                size=actual_size,
                                price=actual_price,
                                fee=0.0,
                                fill_type='MAKER',  # Limit orders are maker
                                fill_id=f"{self.pending_order_id}_fill",
                            )
                            logger.info(f"✅ position_store updated: {fill_side.upper()} {actual_size:.6f} @ {actual_price:.2f}")
                    except Exception as ps_err:
                        logger.warning(f"⚠️ Could not update position_store on fill: {ps_err}")
                    
                    self.risk_engine.create_position(
                        symbol=self.symbol,
                        direction=entry_data['bias'],
                        entry_price=actual_price,
                        position_size=actual_size,
                        stop_loss=entry_data['stop_loss'],
                        atr_1m=entry_data['atr_1m'],
                        market_time=market_time,  # Use market time, not datetime.now()
                        trade_type=entry_data.get('trade_type', TradeType.LONG_MAIN if entry_data['bias'] == 'LONG' else TradeType.SHORT_STRUCTURAL),
                        regime_mode=self._current_trade_regime  # 🔥 FIX (Jan 22): Pass regime to trailing
                    )
                    
                    # 🔥 NEW: Start exhaustion tracking for this trade
                    self.exhaustion_detector.start_tracking(market_time)
                    self._exhaustion_tightened = False
                    
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
                            take_profit=float(entry_data.get('take_profit', 0) or 0),
                            position_size=actual_size,
                            entry_score=entry_data.get('score', 0),
                            entry_tags=list(trade_tags.keys()) if isinstance(trade_tags, dict) else [],
                            regime=trade_tags.get('regime', 'UNKNOWN'),
                            volatility_regime=trade_tags.get('volatility_regime', 'NORMAL'),
                            session=session_for_count,
                            trade_type=trade_type_name,
                            score_breakdown=trade_score_breakdown,
                            metadata={'worst_case_rr': trade_tags.get('rr_worst_case')},
                            # 🔥 ML Data Collection (Jan 24, 2026)
                            feature_snapshot=self._build_ml_feature_snapshot(),
                            entry_trading_mode=trade_tags.get('trading_mode', ''),
                            entry_daily_regime=self._daily_regime
                        )
                        # 🔥 FIX (Jan 27, 2026): Use CENTRALIZED activity tracker
                        # This is the SINGLE source of truth for trade activity
                        from src.domain.trading.activity_tracker import record_trade_executed
                        record_trade_executed(market_time)
                        self._last_trade_time = market_time  # Keep local copy for backwards compat
                        
                        # 🎯 EDGE STATE: Record entry for trade limiting
                        self._edge_manager.record_entry(market_time)
                        logger.debug(f"🎯 Edge entry recorded (trades on this BOS: {self._edge_manager.current_bos.trades_attempted if self._edge_manager.current_bos else 0})")
                    except Exception as log_err:
                        logger.error(f"TradeLogger.start_trade() failed: {log_err} - continuing without trade logging")
                        self._current_trade_id = None
                    
                    # 📊 CAPA 5: Record trade entry in health monitor
                    if self.health_monitor:
                        self.health_monitor.record_trade_entry(
                            side=entry_data['bias'],
                            size=entry_data['position_size'],
                            score=int(self._entry_score)
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
                
                # 🔥 NEW (Jan 28, 2026): Send entry notification to Telegram
                if self.output_manager and hasattr(self, '_last_entry_data') and self._last_entry_data:
                    try:
                        entry_data = self._last_entry_data
                        # 🔥 FIX (Jan 29, 2026 - AUDIT BUG 11): Use entry_data directly instead of undefined locals
                        _create_safe_task(self.output_manager.send_entry_notification({
                            'symbol': self.symbol,
                            'direction': entry_data.get('bias', 'UNKNOWN'),
                            'entry_price': entry_data.get('entry_price'),
                            'stop_loss': entry_data.get('stop_loss'),
                            'take_profit': entry_data.get('take_profit'),
                            'size': entry_data.get('position_size'),
                            'score': self._entry_score,
                            'regime': self._current_trade_regime,
                            'risk_usd': entry_data.get('tags', {}).get('risk_usd'),
                            'timestamp': market_time.isoformat() if hasattr(market_time, 'isoformat') else str(market_time)
                        }), "send_entry_notification")
                        logger.info("📲 Entry notification sent to Telegram")
                    except Exception as notif_err:
                        logger.debug(f"Entry notification error: {notif_err}")
                
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
                self.state_machine.update_daily_pnl(estimated_pnl)  # 🔥 FIX: Update daily PnL (even if 0)
                
                # Limpiar posición local (thread-safe)
                await self.risk_engine.clear_position("reconcile close - no position on exchange")
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
        # � REFACTORED (Jan 27, 2026): PROBING + ANTICIPATION CHECKS
        # Consolidated into PositionManager (AUDIT REFACTOR Point 5)
        # ==================================================================
        
        # Run probing mode check
        probing_result = ProbingModeChecker.check_probing_exit(
            position, self._current_trade_tags, market_time, current_price
        )
        if probing_result.decision == ExitDecision.CLOSE:
            logger.warning(f"🔴 {probing_result.reason}: {probing_result.extra_data}")
            await self._close_position_with_full_flow(
                reason=probing_result.reason,
                current_price=current_price,
                market_time=market_time,
                position=position,
                cooldown_minutes=probing_result.cooldown_minutes,
                extra_log_data=probing_result.extra_data
            )
            return
        
        # Track PROBE_MAX observability (doesn't cause exit, just logs)
        self._current_trade_tags = ProbingModeChecker.track_probe_max_sequence(
            position, self._current_trade_tags, market_time
        )
        
        # Run anticipation timeout check
        anticipation_result = AnticipationChecker.check_anticipation_timeout(
            position, self._current_trade_tags, market_time
        )
        if anticipation_result.decision == ExitDecision.CLOSE:
            await self._close_position_with_full_flow(
                reason=anticipation_result.reason,
                current_price=current_price,
                market_time=market_time,
                position=position,
                cooldown_minutes=anticipation_result.cooldown_minutes,
                extra_log_data=anticipation_result.extra_data
            )
            return

        # ==================================================================
        # ⚡ MOMENTUM EXHAUSTION DETECTOR - Exit at the "summum" (Jan 22, 2026)
        # Detects when momentum is dying BEFORE price reverses
        # ==================================================================
        mfe_r = position.calculate_mfe_r() if position else 0
        
        # Only run exhaustion check if profitable (MFE > 0.5R)
        if mfe_r >= 0.5:
            try:
                candles_1m_exh = self.market_data.get_candles('1m')
                candles_5m_exh = self.market_data.get_candles('5m')
                exhaustion = None  # Initialize to avoid UnboundLocalError
                
                if candles_1m_exh and len(candles_1m_exh) >= 30 and candles_5m_exh and len(candles_5m_exh) >= 30:
                    df_1m_exh = pd.DataFrame(candles_1m_exh)
                    df_5m_exh = pd.DataFrame(candles_5m_exh)
                    
                    exhaustion = self.exhaustion_detector.evaluate(
                        df_1m=df_1m_exh,
                        df_5m=df_5m_exh,
                        direction=position.direction,
                        market_time=market_time,
                        mfe_r=mfe_r
                    )
                    
                    self._last_exhaustion_state = exhaustion
                
                if exhaustion and exhaustion.level == ExhaustionLevel.SEVERE:
                    # SEVERE exhaustion (4+ signals) → Exit now
                    logger.critical(f"⚡ EXHAUSTION EXIT: {exhaustion.signal_count} signals detected!")
                    for sig in exhaustion.signals_detected[:3]:
                        logger.info(f"   {sig}")
                    
                    await self._close_position_with_full_flow(
                        reason="EXHAUSTION_EXIT",
                        current_price=current_price,
                        market_time=market_time,
                        position=position,
                        cooldown_minutes=2,
                        extra_log_data={'exhaustion_signals': exhaustion.signal_count}
                    )
                    return
                
                elif exhaustion and exhaustion.level == ExhaustionLevel.MODERATE:
                    # 🔧 REFACTORED (Jan 27): Use ExhaustionTighteningChecker
                    candles_5m_trail = self.market_data.get_candles('5m')
                    trail_atr = None
                    if candles_5m_trail and len(candles_5m_trail) >= 14:
                        df_trail = pd.DataFrame(candles_5m_trail)
                        trail_atr_vals = calculate_atr(
                            df_trail['high'].to_numpy(), 
                            df_trail['low'].to_numpy(), 
                            df_trail['close'].to_numpy(), 14
                        )
                        trail_atr = trail_atr_vals[-1] if len(trail_atr_vals) > 0 else None
                    
                    tighten_result = ExhaustionTighteningChecker.calculate_tightened_stop(
                        position, current_price, trail_atr, 
                        getattr(self, '_exhaustion_tightened', False)
                    )
                    if tighten_result.decision == ExitDecision.TIGHTEN:
                        logger.warning(f"⚠️ EXHAUSTION WARNING: {exhaustion.signal_count} signals - tightening stop")
                        position.trailing_stop = tighten_result.new_stop
                        logger.info(f"🎯 Trailing tightened: {tighten_result.extra_data.get('old_stop', 0):.2f} → {tighten_result.new_stop:.2f}")
                        self._exhaustion_tightened = True
                        
            except Exception as e:
                logger.warning(f"⚠️ Exhaustion detector error (non-fatal): {e}")
        else:
            self._exhaustion_tightened = False

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
                # � REFACTORED (Jan 27, 2026): Use unified close method
                await self._close_position_with_full_flow(
                    reason="FUNDING_EXIT",
                    current_price=current_price,
                    market_time=market_time,
                    position=position,
                    cooldown_minutes=3,
                    extra_log_data={'funding_reason': funding_exit_reason}
                )
                return

            # FIX #13: time-based exit (stagnation-based, not clock-based)
            # REDESIGNED (Jan 4, 2026): Pass current ATR for stagnation check
            current_atr = position.atr_1m  # Use stored ATR, can be updated
            exit_time, time_exit_reason = position.should_force_exit_by_time(market_time, current_atr)
            if exit_time:
                logger.critical(f"🚨 FORCED EXIT (Time): {time_exit_reason}")
                # 🔧 REFACTORED (Jan 27, 2026): Use unified close method
                await self._close_position_with_full_flow(
                    reason="TIME_EXIT",
                    current_price=current_price,
                    market_time=market_time,
                    position=position,
                    cooldown_minutes=3,
                    extra_log_data={'time_reason': time_exit_reason}
                )
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
        
        # ==================================================================
        # 🔧 REFACTORED (Jan 27, 2026): MOMENTUM DECAY CHECK
        # Consolidated into PositionManager (AUDIT REFACTOR Point 5)
        # ==================================================================
        momentum_result = MomentumDecayChecker.check_momentum_decay(
            position, current_price, market_time, current_adx
        )
        if momentum_result.decision == ExitDecision.TIGHTEN:
            logger.info(f"📉 {momentum_result.reason} | Tightening SL to {momentum_result.new_stop:.2f}")
            position.trailing_stop = momentum_result.new_stop
        
        # Find TP2 target
        tp2_target = self._find_tp2_target()
        
        # Check for BOS against position
        has_bos_against = self._check_bos_against_position(position.direction, candles_5m)
        
        # 🔥 FIX (Jan 23, 2026): Log if this is an orphan recovery BOS check
        if getattr(self, '_force_bos_check_on_orphan', False):
            logger.info(f"📊 ORPHAN BOS CHECK: {position.direction} | has_bos_against={has_bos_against}")
            self._force_bos_check_on_orphan = False  # Reset flag
        
        # ==================================================================
        # 🔧 REFACTORED (Jan 27, 2026): BOS INVALIDATION CHECK
        # Consolidated into PositionManager (AUDIT REFACTOR Point 5)
        # ==================================================================
        df_5m = pd.DataFrame(candles_5m) if candles_5m else None
        bos_result = BOSInvalidationChecker.check_bos_invalidation(
            position, has_bos_against, current_price, market_time, df_5m
        )
        
        if bos_result.decision == ExitDecision.CLOSE:
            self._current_trade_tags['bos_invalidation_close'] = True
            self._current_trade_tags['bos_invalidation_r'] = bos_result.extra_data.get('current_r', 0)
            
            success = await self._close_position_with_full_flow(
                reason=bos_result.reason,
                current_price=current_price,
                market_time=market_time,
                position=position,
                cooldown_minutes=bos_result.cooldown_minutes,
                extra_log_data=bos_result.extra_data
            )
            if success:
                return
                
        elif bos_result.decision == ExitDecision.TIGHTEN:
            position.trailing_stop = bos_result.new_stop
            self._current_trade_tags['bos_invalidation_tighten'] = True
            logger.warning(
                f"⚠️ BOS INVALIDATION: {position.direction} thesis breaking "
                f"(BOS @ {bos_result.extra_data.get('bos_level', 0):.2f}) | "
                f"R: {bos_result.extra_data.get('current_r', 0):.2f} → Tightening SL 50%"
            )
        
        # 🔥 FIX #3: Block logical exits when exchange is unavailable
        # Only emergency close with confirmed position is allowed
        if not self.reconciler.is_exchange_available():
            if self.reconciler.is_degraded:
                logger.warning("⚠️ DEGRADED MODE: Skipping logical exit checks - exchange unavailable")
            return  # Don't make any decisions based on stale data
        
        # =====================================================================
        # 🔥 ANOMALY DETECTION DATA (Jan 24, 2026)
        # Calculate anomaly indicators for EMERGENCY system
        # 🔧 FIX (Jan 25, 2026): Use real feed staleness from market_data
        # =====================================================================
        price_gap_pct = None
        
        # 🔧 FIX (Jan 25, 2026): Use REAL feed staleness from market_data
        # Previous implementation calculated time between tick processing (buggy)
        last_tick_age_sec = self.market_data.get_feed_age_sec()
        
        # Calculate price gap from previous tick (this one works correctly)
        if self._prev_tick_price > 0 and current_price > 0:
            price_gap_pct = ((current_price - self._prev_tick_price) / self._prev_tick_price) * 100
        
        # Update tracking for next tick (price gap calculation still needs prev price)
        self._prev_tick_price = current_price
        self._prev_tick_time = market_time
        
        # =====================================================================
        # �🔒 FIX (Jan 13, 2026): SYNC SIZE FROM RECONCILER SNAPSHOT
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
            atr_ratio=atr_ratio,       # For volatility check (also anomaly detection)
            rsi_1m=rsi_1m,             # For momentum exit
            vpoc=vpoc_price,           # For VPOC resistance exit
            bb_width=bb_width,         # For volatility squeeze
            bb_avg=bb_avg,             # For volatility squeeze comparison
            # 🔥 ANOMALY DETECTION PARAMS (Jan 24, 2026)
            spread_bps=None,           # TODO: Get from order book when available
            spread_normal_bps=3.0,     # BTC/USD typical spread
            last_tick_age_sec=last_tick_age_sec,  # Feed staleness
            price_gap_pct=price_gap_pct  # Price gap anomaly
        )
        
        if exit_action:
            action = exit_action.get('action')

            # ==================================================================
            # 🔧 REFACTORED (Jan 27, 2026): Exit action handlers extracted
            # Consolidated into exit_actions.py (AUDIT REFACTOR)
            # ==================================================================
            try:
                if action == 'PARTIAL_CLOSE':
                    await handle_partial_close(self, exit_action, position, current_price)
                    return

                if action == 'ADD_POSITION':
                    await handle_add_position(self, exit_action, position, current_price)
                    return

                if action == 'FULL_CLOSE':
                    await handle_full_close(self, exit_action, position, current_price, market_time)
                        
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
                    
                    # 🔥 ML DRIFT MONITORING (Jan 24, 2026)
                    # Record outcome for model performance tracking
                    ml_prob = self._current_trade_tags.get('ml_win_prob', 0.5)
                    if self._ml_inference and ml_prob != 0.5:  # Only if ML made prediction
                        drift_result = self._ml_inference.record_outcome(
                            trade_id=self._current_trade_id,
                            ml_prob=ml_prob,
                            actual_win=is_win
                        )
                        if drift_result.get('drift_detected'):
                            logger.warning("⚠️ ML model degradation detected - consider retraining")
                            
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
                self._update_score_metrics(int(self._entry_score), pnl, is_win)
            
            # Record for loss cooldown tracking
            self._record_trade_result(exit_reason, pnl, market_time)
            
            # Record for filter pipeline (overtrade filter zone tracking)
            if hasattr(self, 'filter_pipeline') and self._current_trade_entry_time:
                self.filter_pipeline.record_trade_result(
                    entry_price=self._current_trade_entry_price or position.entry_price,
                    exit_price=current_price,
                    entry_time=self._current_trade_entry_time,
                    exit_time=market_time,
                    direction=position.direction if position else 'LONG',
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
                highs = df_1m['high'].to_numpy()
                lows = df_1m['low'].to_numpy()
                closes = df_1m['close'].to_numpy()
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
                atr_values = calculate_atr(df_5m['high'].to_numpy(), df_5m['low'].to_numpy(), df_5m['close'].to_numpy(), 14)
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
    
    def _find_support_resistance(self, bias: str | None, df_5m: pd.DataFrame) -> float:
        """Find technical support/resistance - delegates to indicator_helpers"""
        return _find_sr_pure(bias or 'LONG', df_5m, lookback=10)
    
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
        # 🔥 FIX: Use _indicator_cache_v2 (centralized cache)
        if not hasattr(self, '_indicator_cache'):
            # Initialize legacy cache dict for backward compatibility
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
                adx_arr, _ = calculate_adx(df_1h['high'].to_numpy(), df_1h['low'].to_numpy(), df_1h['close'].to_numpy())
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
                    atr_values = calculate_atr(df_1m['high'].to_numpy(), df_1m['low'].to_numpy(), df_1m['close'].to_numpy())
                    if len(atr_values) >= 10:
                        cache['atr_1m'] = float(atr_values[-1])
                        avg_atr = float(np.mean(atr_values[-10:]))
                        cache['atr_ratio'] = cache['atr_1m'] / avg_atr if avg_atr > 0 else 1.0
                except Exception as e:
                    logger.debug(f"ATR/ratio calculation failed: {e}")
            
            # RSI
            if len(df_1m) >= 14:
                try:
                    closes = df_1m['close'].to_numpy()
                    rsi_values = calculate_rsi(closes, 14)
                    if len(rsi_values) > 0:
                        cache['rsi_1m'] = float(rsi_values[-1])
                except Exception as e:
                    logger.debug(f"RSI calculation failed: {e}")
            
            # Bollinger Bands
            if len(df_1m) >= 20:
                try:
                    closes = df_1m['close'].to_numpy()
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
                    from src.domain.market.filters import calculate_vpoc
                    cache['vpoc'] = calculate_vpoc(cache['df_1m'].tail(100) if len(cache['df_1m']) >= 100 else cache['df_1m'])
                except Exception as e:
                    logger.debug(f"VPOC calculation failed: {e}")
                    cache['vpoc'] = None
        
        # Update 5m indicators (EMA20) - only when new 5m candle
        if needs_5m_update and cache['df_5m'] is not None and len(cache['df_5m']) >= 20:
            try:
                df_5m = cache['df_5m']
                closes_5m = df_5m['close'].to_numpy()
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
        """Calculate ATR from 5m timeframe - delegates to indicator_helpers"""
        return _calc_atr_5m_pure(df_5m)
    
    def _calculate_atr_generic(self, df: pd.DataFrame, period: int = 14) -> float:
        """Calculate ATR for any timeframe - delegates to indicator_helpers"""
        return _calc_atr_generic_pure(df, period)
    
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
        """ATR REGIME FILTER - delegates to filter_checks module"""
        return _check_atr_regime_pure(df_5m)
    
    def _check_volatility_minimum(self, df_1m: pd.DataFrame) -> tuple[bool, str]:
        """VOLATILITY MINIMUM FILTER - delegates to filter_checks module"""
        return _check_vol_min_pure(df_1m)
    
    def _check_loss_cooldown(self, market_time: datetime) -> tuple[bool, str]:
        """
        LOSS STREAK COOLDOWN + AUTO KILL-SWITCH
        
        🔥 ENHANCED (Jan 25, 2026): Tiered pauses + full kill switch
        - 3 losses → 15 min pause
        - 4 losses → 60 min pause  
        - 5 losses → 180 min pause
        - 6 losses → KILL SWITCH (daily)
        
        Also tracks dead hours losses and blocks after 2+ losses in 4am-7am UTC.
        
        Returns: (can_enter, reason)
        """
        from src.config.trading_config import OperatingMode
        
        # Check KILL SWITCH first (highest priority)
        if self._kill_switch_active:
            return False, "🔴 KILL SWITCH ACTIVE: Daily loss limit reached - no trading until tomorrow"
        
        # Check dead hours block
        if hasattr(self, '_dead_hours_blocked') and self._dead_hours_blocked:
            current_hour = market_time.hour
            dead_start, dead_end = getattr(OperatingMode, 'DEAD_HOURS_UTC', (4, 7))
            if dead_start <= current_hour < dead_end:
                return False, f"🌙 DEAD HOURS BLOCKED: {self._dead_hours_losses} losses in dead zone - waiting for next session"
            else:
                # Reset dead hours block when exiting dead hours
                self._dead_hours_blocked = False
                self._dead_hours_losses = 0
                logger.info("✅ Exited dead hours zone, block lifted")
        
        # Check if we're still in timed cooldown
        if self._cooldown_until is not None:
            if market_time < self._cooldown_until:
                remaining = (self._cooldown_until - market_time).total_seconds() / 60
                
                # SMART COOLDOWN RESET (Jan 15, 2026)
                # Check if micro trend allows early exit (after minimum 5 min)
                min_cooldown_passed = remaining < (self._pause_after_3 - 5)  # At least 5 min in cooldown
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
                
                return False, f"⏸️ LOSS COOLDOWN: {remaining:.0f} min remaining ({self._consecutive_losses} consecutive losses)"
            else:
                # Cooldown expired, reset
                logger.info("✅ Loss cooldown expired, trading resumed")
                self._cooldown_until = None
                self._consecutive_losses = 0
        
        return True, f"No cooldown active (streak: {self._consecutive_losses})"
    
    def _record_trade_result(self, exit_reason: str, pnl: float, market_time: datetime):
        """
        Record trade result for cooldown tracking.
        🔧 REFACTORED (Jan 27, 2026): Delegated to TradeRecorder module.
        """
        from src.application.live.trade_recording import TradeRecordingState
        
        # =====================================================================
        # 🎯 EDGE STATE MANAGER: Record trade result for expectancy tracking
        # =====================================================================
        try:
            # Determine if trade was a win
            is_win = pnl > 0
            
            # Calculate R realized (use stored risk or estimate)
            r_realized = 0.0
            # 🔥 FIX: Use _last_entry_data risk or estimate from position
            if hasattr(self, '_last_entry_data') and self._last_entry_data and 'risk_usd' in self._last_entry_data:
                risk = self._last_entry_data.get('risk_usd', 0)
                if risk > 0:
                    r_realized = pnl / risk
            elif self.risk_engine.has_open_position():
                # Estimate from position's initial stop
                # 🔥 FIX: Access position directly (no get_position method)
                pos = self.risk_engine.position
                if pos:
                    risk_per_unit = abs(pos.entry_price - pos.initial_stop_loss)
                    risk = risk_per_unit * pos.initial_size
                    if risk > 0:
                        r_realized = pnl / risk
            
            # Record in edge manager
            self._edge_manager.record_trade_result(won=is_win, r_captured=r_realized)
            
            logger.debug(f"📊 Edge result recorded: {'WIN' if is_win else 'LOSS'} {r_realized:+.2f}R")
        except Exception as e:
            logger.debug(f"Edge result recording error: {e}")
        
        # Build state from current engine state
        state = TradeRecordingState(
            consecutive_losses=self._consecutive_losses,
            daily_losses_count=self._daily_losses_count,
            loss_exit_types=getattr(self, '_loss_exit_types', {}),
            countertrend_losses_session=self._countertrend_losses_session,
            last_countertrend_loss_time=getattr(self, '_last_countertrend_loss_time', None),
            short_cooldown_until=getattr(self, '_short_cooldown_until', None),
            cooldown_until=getattr(self, '_cooldown_until', None),
            kill_switch_active=getattr(self, '_kill_switch_active', False),
            dead_hours_losses=getattr(self, '_dead_hours_losses', 0),
            dead_hours_blocked=getattr(self, '_dead_hours_blocked', False),
            expansion_trades_count=getattr(self, '_expansion_trades_count', 0),
            false_expansion_count=getattr(self, '_false_expansion_count', 0),
            consecutive_expansion_losses=getattr(self, '_consecutive_expansion_losses', 0),
        )
        
        # Record and get result
        result = self._trade_recorder.record_result(
            exit_reason=exit_reason,
            pnl=pnl,
            market_time=market_time,
            trade_tags=self._current_trade_tags,
            state=state,
            health_monitor=self.health_monitor,
        )
        
        # Update engine state from result
        # 🔥 FIX: Ensure int type for consecutive_losses
        self._consecutive_losses = int(result.new_consecutive_losses)
        self._daily_losses_count = int(result.new_daily_losses_count)
        self._countertrend_losses_session = result.new_countertrend_losses
        
        if result.cooldown_until:
            self._cooldown_until = result.cooldown_until
        if result.kill_switch_activated:
            self._kill_switch_active = True
        if result.dead_hours_blocked:
            self._dead_hours_blocked = True
        if result.short_cooldown_until:
            self._short_cooldown_until = result.short_cooldown_until


    # =========================================================================
    # 🔧 UNIFIED CLOSE POSITION (Jan 27, 2026 - AUDIT REFACTOR Point 4)
    # Eliminates ~150 duplicated lines across 6+ close patterns
    # =========================================================================
    async def _close_position_with_full_flow(
        self,
        reason: str,
        current_price: float,
        market_time: datetime,
        position,  # TrackedPosition or Position object
        cooldown_minutes: int = 3,
        extra_log_data: dict | None = None
    ) -> bool:
        """
        Unified position close with FULL flow:
        1. Reconciler notify
        2. Get REAL size from exchange (avoid orphans)
        3. Execute close
        4. Record PnL in state machine
        5. Update daily PnL
        6. Record for kill-switch
        7. Log trade event
        8. Telegram notification
        9. Clear risk engine
        10. Reset trade state
        11. Start cooldown
        12. Transition to IDLE
        
        Returns: True if close successful, False otherwise
        """
        try:
            # 1. Notify reconciler (hard-lock)
            self.reconciler.notify_exit_started(reason)
            
            # 2. Get REAL size from exchange to avoid orphan positions
            close_size = position.size
            try:
                exchange_pos = await self.execution.get_position(self.symbol)
                if exchange_pos:
                    exchange_size = abs(float(exchange_pos.get('contracts', 0) or 0))
                    if exchange_size > 0:
                        close_size = exchange_size
                        logger.debug(f"Using exchange size: {close_size}")
            except Exception as e:
                logger.debug(f"Could not get exchange position for {reason}: {e} - using local size")
            
            # 3. Validate minimum size
            if close_size < KRAKEN_BTC_MIN_SIZE:
                logger.warning(f"⚠️ {reason} skipped: size {close_size:.6f} < min {KRAKEN_BTC_MIN_SIZE}")
                return False
            
            # 4. Execute close
            close_side = 'sell' if position.direction == 'LONG' else 'buy'
            await self.execution.close_position_market(
                symbol=self.symbol,
                side=close_side,
                amount=close_size,
                reason=reason
            )
            
            # 5. Calculate PnL
            pnl = position.unrealized_pnl
            is_win = pnl > 0
            
            # 6. Update state machine
            self.state_machine.record_trade_close(pnl, is_win, exit_reason=reason)
            self.state_machine.update_daily_pnl(pnl)
            
            # 7. Record for kill-switch tracking
            self._record_trade_result(reason, pnl, market_time)
            
            # 7b. Record trade close in activity tracker (Jan 27 improvement)
            from src.domain.trading.activity_tracker import get_activity_tracker
            pnl_r = position.calculate_r_realized(current_price)
            get_activity_tracker().record_trade_closed(
                market_time=market_time,
                exit_reason=reason,
                pnl_r=pnl_r,
                direction=position.direction
            )
            
            # 8. Log trade event
            base_log_data = {
                'entry': position.entry_price,
                'exit': current_price,
                'size': position.size,
                'pnl': pnl,
                'reason': reason,
                'r': position.calculate_r_realized(current_price),
                'mfe_r': position.calculate_mfe_r(),
                'mae_r': position.calculate_mae_r(),
                'direction': position.direction,
                'regime': getattr(position, 'regime', 'UNKNOWN')
            }
            if extra_log_data:
                base_log_data.update(extra_log_data)
            await self.trade_logger.log_event('trade_closed', base_log_data)
            
            # 9. Telegram notification
            if self.output_manager:
                try:
                    fresh_capital = await self.execution.get_available_capital()
                    if fresh_capital > 0:
                        self.risk_engine.capital = fresh_capital
                except Exception as e:
                    logger.debug(f"Capital refresh error (using existing): {e}")
                
                _create_safe_task(self.output_manager.send_exit_notification({
                    'symbol': self.symbol,
                    'reason': reason,
                    'pnl': pnl,
                    'r_realized': position.calculate_r_realized(current_price),
                    'exit_price': current_price,
                    'timestamp': market_time.isoformat(),
                    'account_value': self.risk_engine.capital
                }), f"{reason.lower()}_notification")
            
            # 10. Clear risk engine
            await self.risk_engine.clear_position(reason)
            
            # 11. Reset trade state
            self._reset_trade_state(f"Trade closed: {reason}")
            
            # 12. Cooldown and transition
            self.state_machine.start_cooldown(cooldown_minutes)
            self.state_machine.transition_to(BotState.IDLE, f"Trade closed: {reason}")
            
            logger.info(f"✅ {reason} EXIT: PnL={pnl:.2f}")
            return True
            
        except Exception as e:
            logger.error(f"❌ {reason} close failed: {e}")
            return False
    
    # =========================================================================
    # 🔧 REFACTORED: _check_expected_r, _calculate_chase_atr moved to market_analysis.py
    # Now accessed via self._expected_r_calculator and calculate_chase_atr_helper()
    # =========================================================================
    
    def _check_not_post_impulse(self, df_1m: pd.DataFrame, atr_1m: float) -> tuple[bool, str]:
        """ANTI-IMPULSE FILTER - delegates to filter_checks module"""
        from src.config.trading_config import OrderExecution
        return _check_post_impulse_pure(
            df_1m, atr_1m,
            impulse_threshold=OrderExecution.ANTI_CHASING_ATR_THRESHOLD,
            lookback=OrderExecution.ANTI_CHASING_LOOKBACK
        )
    
    def _check_sweep_occurred(self, df_5m: pd.DataFrame, bias: str) -> tuple[bool, str]:
        """SWEEP FILTER - delegates to filter_checks module"""
        return _check_sweep_pure(df_5m, bias)
    
    # =========================================================================
    # 🔧 REFACTORED (Jan 27, 2026): Bias Strength, Trend Expansion, Probing, Short Obstinacy
    # All moved to src/application/live/market_analysis.py
    # Now accessed via:
    # - self._bias_strength_detector.detect()
    # - self._trend_expansion_detector.detect()
    # - self._probing_checker.check()
    # - self._short_obstinacy_calculator.calculate()
    # - self._expected_r_calculator.check()
    # =========================================================================
    
    # =========================================================================
    # ENTRY SCORE SYSTEM - Delegated to EntryScorer (Jan 20, 2026)
    # 4 blocks × 0-2 points = max 8. Threshold: 5
    # Only Structure=0 blocks entry (the only real gate)
    # =========================================================================
    
    def _calculate_entry_score(self, df_1h: pd.DataFrame, df_5m: pd.DataFrame, 
                                df_1m: pd.DataFrame | None, bias: str | None, current_price: float,
                                market_time: datetime) -> tuple[int, dict, bool]:
        """
        Calculate entry score using 4 blocks.
        Delegated to EntryScorer module to reduce god class complexity.
        
        Returns: (total_score, breakdown_dict, structure_blocks)
        If structure_blocks=True, DO NOT ENTER regardless of score.
        """
        # Handle None df_1m with empty DataFrame
        if df_1m is None:
            df_1m = pd.DataFrame()
        
        # Delegate to EntryScorer
        score, breakdown, structure_blocks = self._entry_scorer.calculate_entry_score(
            df_1h=df_1h,
            df_5m=df_5m,
            df_1m=df_1m,
            bias=bias or 'LONG',
            current_price=current_price,
            market_time=market_time,
            micro_trend_state=getattr(self, '_micro_trend_state', None),
            filter_pipeline=getattr(self, 'filter_pipeline', None),
        )
        
        # Copy trade tags from scorer to signal engine for backwards compatibility
        scorer_tags = self._entry_scorer.trade_tags
        self._current_trade_tags = {
            'score': scorer_tags.score,
            'mode': scorer_tags.mode,
            'htf_aligned': scorer_tags.htf_aligned,
            'ltf_clean': scorer_tags.ltf_clean,
            'bos_confirmed': scorer_tags.bos_confirmed,
            'ema_reclaim': scorer_tags.ema_reclaim,
            'adx': scorer_tags.adx,
            'adx_5m': scorer_tags.adx_5m,
            'adx_slope': scorer_tags.adx_slope,
            'adx_slope_bucket': scorer_tags.adx_slope_bucket,
            'rsi': scorer_tags.rsi,
            'atr': scorer_tags.atr,
            'session': scorer_tags.session,
            'session_quality': scorer_tags.session_quality,
            'volatility_ratio': scorer_tags.volatility_ratio,
            'volume_penalty': scorer_tags.volume_penalty,
            'slope_penalty': scorer_tags.slope_penalty,
            'penalty_hierarchy_saved': scorer_tags.penalty_hierarchy_saved,
            'worst_context_penalty': scorer_tags.worst_context_penalty,
            'worst_execution_penalty': scorer_tags.worst_execution_penalty,
        }
        
        # Store ADX values in score breakdown for regime detection
        scorer_breakdown = self._entry_scorer.breakdown
        self._score_breakdown['adx_values'] = scorer_breakdown.adx_values
        
        return score, breakdown, structure_blocks
    
    # =========================================================================
    # SCORING METHODS MOVED TO src/engine/scoring/entry_scorer.py (Jan 20, 2026)
    # The following methods were extracted to reduce god class complexity:
    # - _apply_penalty_hierarchy
    # - _apply_rr_score_adjustment  
    # - _score_structure, _score_timing, _score_momentum, _score_context
    # - _get_score_size_multiplier
    # Now accessed via self._entry_scorer
    # =========================================================================
    
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
        """SESSION FILTER: 24/7 trading for BTC"""
        return True, f"Session OK: 24/7 trading ({market_time.hour}:00 UTC)"
    
    def _check_trend_filter(self, df: pd.DataFrame, bias: str, current_price: float) -> tuple[bool, str]:
        """TREND FILTER - delegates to filter_checks module"""
        return _check_trend_pure(df, bias, current_price)
    
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
        
        closes = df['close'].to_numpy()
        highs = df['high'].to_numpy()
        lows = df['low'].to_numpy()
        volumes = df['volume'].to_numpy() if 'volume' in df.columns else None
        
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
        """VOLATILITY FILTER - delegates to filter_checks module"""
        return _check_volatility_expansion_pure(df)
    
    # 🔥 REMOVED: _check_not_compressed - was disabled (always returned True)
    
    def _check_anti_reverso(self, df: pd.DataFrame, bias: str) -> tuple[bool, str]:
        """ANTI-REVERSO FILTER - delegates to filter_checks module"""
        return _check_anti_reversal_pure(df, bias)
    
    # 🔥 REMOVED: _check_not_duplicate_zone - was disabled (always returned True)
    
    def _check_volume_confirmation(self, df: pd.DataFrame) -> tuple[bool, str]:
        """DELTA VOLUME FILTER - delegates to filter_checks module"""
        return _check_volume_delta_pure(df)
    
    def _build_ml_feature_snapshot(self) -> dict[str, float]:
        """
        Build feature snapshot for ML training data collection.
        
        🔥 ML Data Collection (Jan 24, 2026)
        Captures all relevant indicators at entry moment for:
        - Regime prediction model training
        - Entry quality model training
        - Exit timing model training
        
        Returns:
            Dict with all ML-relevant features matching ENTRY_FEATURES list
        """
        features = {}
        
        try:
            # From current trade tags (already computed)
            tags = self._current_trade_tags
            
            # === Score components (from _calculate_and_evaluate_entry_score) ===
            features['score_structure'] = float(tags.get('score_structure', 1.0))
            features['score_timing'] = float(tags.get('score_timing', 1.0))
            features['score_momentum'] = float(tags.get('score_momentum', 1.0))
            features['score_context'] = float(tags.get('score_context', 1.0))
            features['score_synergy'] = float(tags.get('score_synergy', 0.0))
            
            # === Trend indicators ===
            features['adx_1h'] = float(tags.get('htf_adx', tags.get('adx_1h', 20.0)))
            features['adx_5m'] = float(tags.get('adx_5m_fast', tags.get('adx_5m', 20.0)))
            features['effective_adx'] = float(tags.get('effective_adx', features['adx_1h']))
            
            # === Volatility ===
            features['atr_ratio'] = float(tags.get('atr_ratio', tags.get('volatility_ratio', 1.0)))
            features['bb_width'] = float(tags.get('bb_width', 300.0))
            features['bb_squeeze'] = 1.0 if tags.get('bb_squeeze', False) else 0.0
            
            # === Location/Context ===
            features['percentile_60d'] = float(tags.get('percentile_60d', tags.get('location_pct_60d', 50.0)))
            features['location_headroom_atr'] = float(tags.get('location_headroom_atr', 5.0))
            features['location_in_extreme'] = 1.0 if tags.get('location_in_extreme', False) else 0.0
            
            # === Momentum ===
            features['rsi_14'] = float(tags.get('rsi', tags.get('rsi_14', 50.0)))
            features['cvd_bullish'] = 1.0 if tags.get('cvd_bullish', False) else 0.0
            features['volume_ratio'] = float(tags.get('volume_ratio', 1.0))
            
            # === Session (one-hot) ===
            session = tags.get('session_quality', 'moderate')
            features['session_good'] = 1.0 if session == 'good' else 0.0
            features['session_poor'] = 1.0 if session == 'poor' else 0.0
            
            # === Regime (one-hot) ===
            trading_mode = tags.get('trading_mode', tags.get('regime', 'RANGE'))
            features['trading_mode_trend'] = 1.0 if trading_mode in ('TREND', 'TREND_EXPANSION') else 0.0
            features['trading_mode_range'] = 1.0 if trading_mode in ('RANGE', 'MEAN_REVERSION') else 0.0
            features['trading_mode_transition'] = 1.0 if trading_mode in ('TRANSITION', 'TREND_EXHAUSTION') else 0.0
            
            # === External data ===
            features['funding_rate'] = float(tags.get('funding_rate', 0.0))
            
            # === Entry quality ===
            features['chase_atr'] = float(tags.get('chase_atr', 0.0))
            
            # === Time features (BTC has strong intraday/weekly patterns) ===
            from datetime import datetime
            market_time = self.market_data.get_market_time() or datetime.utcnow()
            features['day_of_week'] = float(market_time.weekday())  # 0=Monday, 6=Sunday
            features['hour_utc'] = float(market_time.hour)          # 0-23 UTC
            
            # === Trade history ===
            features['consecutive_losses'] = float(self._consecutive_losses)
            
        except Exception as e:
            logger.warning(f"⚠️ ML feature snapshot error: {e}")
        
        return features
    
    def _record_trade_price(self, entry_price: float):
        """Record trade price for duplicate zone detection"""
        if not hasattr(self, '_recent_trade_prices'):
            self._recent_trade_prices = []
        self._recent_trade_prices.append(entry_price)
        # Keep only last 5
        self._recent_trade_prices = self._recent_trade_prices[-5:]
    
    def _find_tp2_target(self) -> float:
        """Find TP2 target - delegates to indicator_helpers"""
        candles_5m = self.market_data.get_candles('5m')
        if not candles_5m:
            return 0
        
        df_5m = pd.DataFrame(candles_5m)
        
        # Use position direction, not current_bias
        position = self.risk_engine.position
        direction = position.direction if position else self.current_bias
        
        return _find_tp2_pure(df_5m, direction or 'LONG')
    
    def _check_bos_against_position(self, direction: str, candles_5m: list) -> bool:
        """BOS detection - delegates to filter_checks module with edge-triggered logging"""
        result = _check_bos_pure(direction, candles_5m)
        
        # Edge-triggered logging (only log once per new BOS level)
        if result and candles_5m:
            df = pd.DataFrame(candles_5m)
            if direction == 'LONG':
                level = df['low'].iloc[-1]
                if self._last_bos_level != level or self._last_bos_direction != 'LONG':
                    logger.info(f"📉 BOS detectado (LONG): LL @ {level:.2f}")
                    self._last_bos_level = level
                    self._last_bos_direction = 'LONG'
            else:
                level = df['high'].iloc[-1]
                if self._last_bos_level != level or self._last_bos_direction != 'SHORT':
                    logger.info(f"📈 BOS detectado (SHORT): HH @ {level:.2f}")
                    self._last_bos_level = level
                    self._last_bos_direction = 'SHORT'
        
        return result
    
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
                    await self.risk_engine.clear_position("Position too small to close")
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
                # 🔒 FIX (Jan 22, 2026): Also update daily PnL for DD tracking
                self.state_machine.update_daily_pnl(pnl)
                
                # 🔥 FIX (Jan 25, 2026): Record for kill-switch tracking
                from datetime import datetime, UTC
                self._record_trade_result('EMERGENCY_CLOSE', pnl, datetime.now(UTC))
                
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
                
                await self.risk_engine.clear_position("EMERGENCY_CLOSE")
                
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
            
            # Step 4: Clear risk engine position (thread-safe)
            if self.risk_engine.position:
                await self.risk_engine.clear_position("daily halt emergency close")
            
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
    
    def get_important_levels_info(self, current_price: float | None = None) -> str:
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
            highs = df_1h['high'].to_numpy()
            lows = df_1h['low'].to_numpy()
            closes = df_1h['close'].to_numpy()
            volumes = df_1h['volume'].to_numpy()
            
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
            highs = df_1h['high'].to_numpy()
            lows = df_1h['low'].to_numpy()
            closes = df_1h['close'].to_numpy()
            volumes = df_1h['volume'].to_numpy()
            
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
                volume_profile_quality=float(vol_quality),
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
                        
                        # Clear local state (thread-safe)
                        if self.risk_engine.position:
                            logger.warning("🔄 Clearing ghost position from local state")
                            await self.risk_engine.clear_position("ghost position - exchange has none")
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
                        self.state_machine.update_daily_pnl(pnl_estimate)  # 🔥 FIX: Update daily PnL tracking
                        
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
                        
                        # Clear local state (thread-safe)
                        await self.risk_engine.clear_position("phantom position cleared by exchange")
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
                    await self.risk_engine.clear_position("periodic reconciliation - no exchange position")
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

