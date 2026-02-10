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
    SpreadFilter, get_spread_filter,  # 🔥 REMOVED Feb 04: check_spread_for_entry, SpreadCheckResult - unused
)
from src.domain.risk.risk_engine import RiskEngine
from src.infrastructure.exchanges.kraken.execution import round_size_to_exchange, KRAKEN_BTC_MIN_SIZE  # 🔒 FIX: Centralized size rounding + min size
from src.shared.indicators import (
    calculate_ema, calculate_atr, calculate_adx, calculate_rsi, calculate_bollinger_bands,
    calculate_adx_with_di  # 🔥 FIX #83: For RegimeFusion +DI/-DI
)
from src.config.trading_config import HTFFilters, RiskManagement, OperatingMode  # For logging thresholds + MODE
from src.config.trading_config import DailyRegimeDetector  # 🔥 FIX (Jan 23): Daily regime classification
from src.domain.market.regime import EdgeRegimeClassifier, RegimeDetector, EntryRegime  # 🔥 REMOVED Feb 04: calculate_chase_atr - unused
from src.domain.market.regime import TrendIntelligence, TrendPhase, get_trend_intelligence  # 🔥 NEW: Trend cycle detection
from src.domain.trading.edge.edge_enforcement import EdgeEnforcement
from src.domain.trading.edge.edge_validator import ContextValidity
# NOTE: PositionSizer class removed - using risk_engine for sizing
from src.shared.time_provider import get_time_provider  # 🔥 REMOVED Feb 04: TimeProvider - unused
from src.infrastructure.monitoring.error_tracker import get_error_tracker, ErrorSeverity
from src.infrastructure.monitoring.system_health import SystemHealthMonitor
from src.infrastructure.exchanges.kraken.exchange_reconciler import ExchangeReconciler  # 🔥 REMOVED Feb 04: ReconcilerState - unused
from src.infrastructure.monitoring.opportunity_tracker import MissedOpportunityTracker
from src.domain.market.structure.important_levels import (
    ImportantLevelsProvider, get_important_levels_provider,
    initialize_important_levels, LevelContext
    # 🔥 REMOVED Feb 04: LevelType - unused
)
from src.domain.trading.strategy.pullback_short import (
    get_pullback_short_engine,
    PullbackShortSignal, TradeType
    # 🔥 REMOVED Feb 04: PullbackShortEngine - unused
)
from src.config.trading_config import PullbackShortConfig

# NEW: Bias Strength & Trend Expansion configs (Jan 22, 2026)
# 🔥 FIX #76 (Feb 07, 2026): Added WorkingBias for preference-based trading
from src.config.trading_config import (
    BiasStrength, BiasStrengthConfig, TrendExpansionConfig, WorkingBias
    # 🔥 REMOVED Feb 04: ShortObstinacyConfig - unused
)

# 🔥 FIX #83 (Feb 07, 2026): 3-Layer Regime Fusion Architecture
from src.domain.market.regime.regime_fusion import RegimeFusion, TradeDecision

# NEW: Anticipation & Exhaustion engines (Jan 22, 2026)
from src.domain.trading.anticipation import (
    get_anticipation_engine, AnticipationState
    # 🔥 REMOVED Feb 04: AnticipationEngine, AnticipationSignal - unused
)
from src.domain.trading.strategy.momentum_exhaustion import (
    get_exhaustion_detector, ExhaustionLevel, ExhaustionState
    # 🔥 REMOVED Feb 04: MomentumExhaustionDetector - unused
)

from src.infrastructure.monitoring.metrics import (
    update_pullback_short_signal, update_pullback_short_entry,
    update_pullback_short_exit, update_pullback_short_status
)

# NEW IMPORTS: Slippage, Trade Logger, Dynamic Configs (Jan 16, 2026)
from src.infrastructure.exchanges.kraken.slippage_model import (
    SlippageModel, get_slippage_model
    # 🔥 REMOVED Feb 04: calculate_worst_case_rr, OrderType - unused
)
from src.infrastructure.storage.parquet.trade_logger import TradeLogger, get_trade_logger
from src.config.trading_config import (
    VolatilityRegime, VolatilityRegimeValue,
    LocationFilterOverride, MicroTrendADXConfig,
    get_session_name
    # 🔥 REMOVED Feb 04: DynamicScoreThreshold, SessionTradeLimits, LossStreakConfig - unused
)
from src.domain.trading.scoring import EntryScorer, get_entry_scorer

# Extracted helper modules (Jan 20, 2026 - Phase 4 refactoring)
# 🔥 CLEANED Feb 04: Removed 4 unused imports (_check_trend_pure, _check_volatility_expansion_pure, 
#                    _check_anti_reversal_pure, _check_volume_delta_pure)
# 🔥 FIX #127 (Feb 09, 2026): Unified BOS detection - use position_manager version
from src.application.live.position_manager import (
    check_bos_against_position as _check_bos_unified,
)
# NOTE: filter_checks.check_bos_against_position is now deprecated
from src.shared.indicator_helpers import (
    calculate_atr_1m as _calc_atr_1m_pure,
    calculate_atr_5m as _calc_atr_5m_pure,
    # 🔥 REMOVED Feb 04: calculate_atr_generic - unused
    find_tp2_target as _find_tp2_pure,
    find_support_resistance as _find_sr_pure,
)

# 🔥 NEW (Jan 22, 2026): Entry Quality Gate - Hard rules before scoring
from src.domain.trading.execution_logic.entry_quality import (
    get_entry_quality_gate
    # 🔥 REMOVED Feb 04: EntryQualityGate, GateDecision, GateResult - unused
)

# NEW (Jan 24, 2026): Intelligent Filter - Multi-layer decision system
from src.domain.trading.execution_logic.intelligent_filter import (
    get_intelligent_filter, FilterSeverity
    # 🔥 REMOVED Feb 04: IntelligentFilter - unused
)

# NEW (Feb 2, 2026): Fast Reversal Detector - React to trend reversals in real-time
from src.domain.market.filters.fast_reversal_detector import (
    get_fast_reversal_detector, check_immediate_flip
    # 🔥 REMOVED Feb 04: ReversalSignal - unused
)

# NEW (Feb 2, 2026): Direction Orchestrator - Unified direction determination
from src.domain.trading.direction_orchestrator import (
    get_direction_orchestrator
    # 🔥 REMOVED Feb 04: DirectionOrchestrator, OrchestrationResult - unused
)

# 🔥 REMOVED (Feb 05, 2026): UnifiedTradingIntelligence - 100% dead code
# It was synced but NEVER queried. DirectionOrchestrator is the SINGLE SOURCE.
# Also removed: RegimeTradingRules (never called)

from src.config.unified_constants import (
    REGIME, SIZING, ADX
    # 🔥 REMOVED Feb 04: TIME, RR, SESSION, MOMENTUM, SCORES, PRICE,
    #                    unified_get_session, get_session_multiplier, get_size_after_losses, 
    #                    get_regime_min_score, get_regime_size_mult - unused
)

# 🔥 NEW (Feb 04, 2026): Regime handlers - extracted from _handle_idle_state
from src.application.live.regime_handlers import (
    calculate_regime_score, calculate_intraday_edge_data, 
    log_regime_score_if_discrepancy
    # 🔥 REMOVED Feb 04: RegimeScoreResult - unused
)

# 🔥 NEW (Feb 04, 2026): Direction state management - extracted from _handle_idle_state
from src.application.live.direction_state_manager import (
    DirectionStateManager
    # 🔥 REMOVED Feb 04: DirectionState, AdaptiveParams - unused
)
from src.application.live.displacement_detector import (
    DisplacementDetector, MicroMomentumDetector
    # 🔥 REMOVED Feb 04: DisplacementResult - unused
)
from src.application.live.bias_invalidation_handler import (
    BiasInvalidationHandler, ExpansionActivationHandler
)

# 🔥 NEW (Feb 04, 2026): Extracted handlers
from src.application.live.indicator_cache import IndicatorCache
from src.application.live.exit_executor import ExitExecutor
from src.application.live.displacement_handler import DisplacementHandler
from src.application.live.cooldown_manager import CooldownManager, get_cooldown_manager
from src.application.live.wait_fill_handler import (
    WaitFillHandler, get_wait_fill_handler, FillHandlerContext
)

# NEW (Jan 24, 2026): Adaptive Entry System - Market-aware thresholds
from src.domain.trading.strategy.adaptive_entry import (
    get_adaptive_entry_system
    # 🔥 REMOVED Feb 04: AdaptiveEntrySystem, MarketCondition - unused
)

# NEW (Jan 24, 2026): ML Entry Score Refinement - Shadow mode logging
from src.ml.inference import get_ml_inference_engine

# NEW ARCHITECTURE: Position Store as Single Source of Truth
from src.bootstrap.runtime import RuntimeMode, get_runtime_config
from src.infrastructure.storage import get_position_store  # 🔥 REMOVED Feb 04: TrackedPosition - unused
from src.domain.trading.cost.cost_model import get_cost_model  # 🔥 FIX (Jan 24): Cost validator

# 🔥 NEW (Jan 27, 2026 - AUDIT REFACTOR Point 3): Centralized indicator caching
from src.application.live.indicator_cache import get_indicator_cache  # Canonical location

# 🔥 NEW (Jan 27, 2026 - AUDIT REFACTOR Point 5): Position management utilities
from src.application.live.position_manager import (
    check_bos_against_position,
    calculate_smart_trailing_stop,
    get_position_manager,
    ProbingModeChecker, AnticipationChecker, MomentumDecayChecker, BOSInvalidationChecker,
    ExitDecision,
    ExhaustionTighteningChecker
    # 🔥 REMOVED Feb 04: calculate_current_r, get_position_health_checker, PositionManager, ExitCheckResult - unused
)

# 🔥 REMOVED Feb 04: Entry execution utilities (ExecutionGuards, PositionSizeCalculator, 
#                    LocationGuards, StopLossCalculator) - unused in trading_app.py

# 🔥 NEW (Jan 27, 2026 - AUDIT REFACTOR): Entry validators
from src.application.live.entry_validators import (
    get_chase_validator,
    get_graduated_sizer,
    get_rr_validator,
    get_cost_validator,
    get_momentum_validator,
    # 🔥 REMOVED Feb 04: ChaseValidator, GraduatedSizer, RRValidator, CostValidator, 
    #                    MomentumValidator, ChaseResult, DefensiveSizingResult, 
    #                    RRValidationResult, CostValidationResult - unused
)

# 🔧 AUDIT REFACTOR (Jan 27, 2026): Exit action handlers
from src.application.live.exit_actions import (
    handle_partial_close,
    handle_add_position,
    handle_full_close
)

# � REMOVED Feb 04: Entry evaluation helpers (check_pre_entry_conditions, update_daily_regime, 
#                    PreEntryCheckResult) - unused in trading_app.py

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
    get_bias_strength_detector,
    get_trend_expansion_detector,
    get_probing_checker,
    get_short_obstinacy_calculator,
    get_expected_r_calculator,
    # 🔥 REMOVED Feb 04: calculate_chase_atr_helper, BiasStrengthDetector, TrendExpansionDetector, 
    #                    ProbingCriteriaChecker, ShortObstinacyCalculator, ExpectedRCalculator, 
    #                    BiasStrengthResult, TrendExpansionResult, ProbingResult, ExpectedRResult - unused
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
# 🔥 Extended (Feb 04, 2026): Added RangeEdgeCriteria for flexible RANGE edge
from src.domain.trading.edge_state_manager import (
    EdgeStateManager, get_edge_manager, EdgeState, EdgeType, RangeEdgeCriteria
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

# =============================================================================
# 🔥 FIX #135: SIMPLE_MODE ELIMINATED (Feb 10, 2026)
# =============================================================================
# The SIMPLE_MODE flag disabled several features that were causing bad trades.
# Those features have now been COMPLETELY REMOVED from the codebase:
# - ❌ REMOVED: SHORT OBSTINACY blocks
# - ❌ REMOVED: BIAS INVERSION
# - ❌ REMOVED: EMA Distance VETO (converted to context tagging)
# - ❌ REMOVED: Micro Trend VETO (converted to context tagging)
#
# The constant SIMPLE_MODE = True is no longer needed.
# Code now runs directly without conditional branches.
# =============================================================================


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
        # 🔄 FAST REVERSAL DETECTOR (Feb 2, 2026) - Real-time trend reversal detection
        # Detects single-candle reversals based on engulfing, strong body, volume
        # Overrides lagging indicators when reversal is detected
        # =====================================================================
        self.fast_reversal_detector = get_fast_reversal_detector()
        
        # =====================================================================
        # 🎭 DIRECTION ORCHESTRATOR (Feb 2, 2026) - Unified direction determination
        # Consolidates ALL direction signals into a single coherent decision.
        # Priority: EXPANSION > DISPLACEMENT > FAST_REVERSAL > MICRO > STRUCTURAL
        # =====================================================================
        self.direction_orchestrator = get_direction_orchestrator()
        
        # =====================================================================
        # 🔥 REMOVED (Feb 05, 2026): UnifiedTradingIntelligence was 100% dead code
        # - It was synced but NEVER queried
        # - DirectionOrchestrator is the SINGLE SOURCE OF TRUTH for direction
        # - RegimeTradingRules was never called anywhere
        # =====================================================================
        
        # 🔥 NEW (Feb 04, 2026): Extracted direction state management
        self.direction_state_mgr = DirectionStateManager()
        self.displacement_detector = DisplacementDetector()
        self.micro_momentum_detector = MicroMomentumDetector()
        self.bias_invalidation_handler = BiasInvalidationHandler()
        self.expansion_activation_handler = ExpansionActivationHandler()
        
        # 🔥 NEW (Feb 04, 2026 - AUDIT FIX): Unified cooldown management
        # Consolidates: _cooldown_until, _rejection_cooldown_until, _short_cooldown_until
        self.cooldown_manager = get_cooldown_manager()

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
        
        # 🔥 FIX #41: Rate limiting for repetitive state logs
        self._last_countertrend_log: datetime | None = None  # Rate limit countertrend logs
        self._last_antifade_log: datetime | None = None  # Rate limit anti-fade logs
        self._last_edge_state_log: str | None = None  # Last edge state string (only log on change)
        self._log_rate_limit_interval = timedelta(minutes=2)  # Default rate limit interval
        
        # 🔧 FIX #129 (Feb 09, 2026): Throttle repetitive evaluation logs
        self._last_vol_divergence_log: datetime | None = None  # Rate limit VOLUME DIVERGENCE logs
        self._last_transition_mode_log: datetime | None = None  # Rate limit TRANSITION_MODE logs
        
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
        
        # 🔥 NEW (Feb 04, 2026): Warmup tracking - prevent trading immediately after startup
        self._warmup_complete = False
        self._candles_since_start = 0
        self._startup_time = datetime.now()
        
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
        #
        # 🔄 DEPRECATED Feb 04: Use DirectionStateManager.state instead
        # These variables are kept for backward compatibility but should be
        # accessed via self.direction_manager.state.* for consistency.
        # See: DirectionStateManager.validate_direction() for unified checks.
        # =====================================================================
        self._structural_bias: str | None = None  # HTF bias (doesn't block trades)
        self._active_direction: str | None = None  # Current momentum direction
        self._forbidden_direction: str | None = None  # Direction blocked by invalidation
        self._displacement_bypass: bool = False  # Bypass filters due to strong displacement
        self._strong_displacement: bool = False  # Strong displacement overrides BIAS FLIP
        
        # 🧠 TrendIntelligence decision outputs (Feb 06, 2026)
        self._ti_size_multiplier: float = 1.0  # TI-recommended size multiplier
        self._ti_score_adjustment: float = 0.0  # TI-recommended score adjustment
        
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
        # 🔥 FIX #19 (Feb 02, 2026): Reducido de 45 a 20 min base
        # El sistema quedaba bloqueado demasiado tiempo cuando el mercado revertía
        self._expansion_timeout_base: int = 20  # Base timeout (was 45)
        self._expansion_timeout_minutes: int = 20  # Actual (recalculado dinámicamente)
        self._current_volatility_ratio: float = 1.0  # ATR_actual / ATR_promedio
        
        # =====================================================================
        # 🔥 REGIME BLOCK TRACKING (Feb 05, 2026) - Coordinación con DEFENSIVE MODE
        #
        # Cuando hay un regime block activo (RANGE sin extremo, etc.), el sistema
        # NO debe activar DEFENSIVE MODE ya que sería redundante.
        # Estos atributos trackean el estado del bloqueo de régimen.
        # =====================================================================
        self._regime_blocked: bool = False  # Si hay bloqueo de régimen activo
        self._regime_block_reason: str = ""  # Razón del bloqueo
        
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
        #
        # 🔄 DEPRECATED Feb 04: Use DirectionStateManager.state.direction_lock*
        # These variables are kept for backward compatibility.
        # =====================================================================
        self._direction_lock: str | None = None  # LONG/SHORT/None
        self._direction_lock_reason: str | None = None  # "DISPLACEMENT"/"STRONG_MOVE"/etc
        self._direction_lock_expires_at: datetime | None = None  # Cuando expira el lock
        self._direction_lock_initial_move: float = 0.0  # Movimiento que causó el lock
        # 🌊 BE WATER: Lock timeout ADAPTATIVO
        # 🔥 FIX #19 (Feb 02, 2026): Reducido de 30 a 15 min base
        self._direction_lock_timeout_base: int = 15  # Base timeout (was 30)
        self._direction_lock_timeout_minutes: int = 15  # Actual (recalculado)
        
        # 🌊 BE WATER: Invalidación asimétrica ADAPTATIVA por régimen
        # 🔥 FIX #19 (Feb 02, 2026): Reducido de 3.0x a 2.0x base
        # RANGE: 1.5x (más fácil invalidar - micro moves importan)
        # TREND: 2.0x (normal)
        # EXPANSION: 2.5x (más difícil - pero no imposible)
        self._asymmetric_invalidation_base: float = 2.0  # Base (was 3.0)
        self._asymmetric_invalidation_mult: float = 2.0  # Actual (recalculado)
        
        # 🌊 BE WATER: Last known volatility ratio for adaptive calculations
        self._last_vol_ratio: float = 1.0  # atr_5m / atr_1m
        self._last_trading_mode: str = 'TREND'  # RANGE/TREND/TRANSITION
        
        # =====================================================================
        # 🧠 DISPLACEMENT MEMORY (Fix #20 - Feb 03, 2026)
        # 
        # PROBLEMA DETECTADO:
        # - Displacement SHORT detectado @ 04:22:02 ($292 move)
        # - Precio sube $197 → BIAS FLIP invalida displacement @ 04:24:02
        # - SHORT OBSTINACY bloquea 14 señales → MISSED +11R
        # - Pero los indicadores (VWAP, EMA) son LAGGING - tardan en actualizar
        #
        # SOLUCIÓN: "Displacement Memory"
        # - Cuando displacement se invalida, guardar memoria de que HUBO uno reciente
        # - Durante X minutos, el OBSTINACY pasa a modo SOFT (no HARD BLOCK)
        # - Esto permite capturar continuaciones cuando indicadores lagging mienten
        # =====================================================================
        self._displacement_memory_direction: str | None = None  # LONG/SHORT/None
        self._displacement_memory_expires_at: datetime | None = None  # Cuando expira la memoria
        self._displacement_memory_amount: float = 0.0  # Tamaño del displacement original
        self._displacement_memory_window_minutes: int = 5  # Ventana de memoria post-invalidación
        
        # =====================================================================
        # 🔥 UNIFIED TRADING DECISION (Feb 04, 2026)
        # Single source of truth for "should we trade / bypass filters"
        # Replaces scattered self._displacement_bypass or self._expansion_mode checks
        # =====================================================================
        
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
        # 🔥 FIX #76 (Feb 07, 2026): Added working_bias for preference-based trading
        # 🔥 FIX #83 (Feb 07, 2026): 3-Layer Regime Fusion Architecture
        # =====================================================================
        self._current_bias_strength: 'BiasStrength | None' = None  # MACRO: BULL_STRONG, BEAR_STRONG, NEUTRAL
        self._working_bias: 'WorkingBias' = WorkingBias.BOTH  # 🔥 FIX #76: WORKING: LONG_PREF, SHORT_PREF, BOTH
        
        # 🔥 FIX #83: 3-Layer Regime Fusion (MACRO + MICRO = TradeDecision)
        self._regime_fusion = RegimeFusion()
        self._last_regime_decision: TradeDecision | None = None
        self._last_logged_macro_regime = None  # For change detection logging
        self._is_bullish_structure: bool | None = None  # 🔥 FIX #70d: True if price > EMA50_1h
        self._trend_expansion_active: bool = False
        self._trend_expansion_start_time: datetime | None = None
        self._anti_fade_until: datetime | None = None  # No counter-trend until this time
        self._countertrend_losses_session: int = 0  # CT losses this session
        self._last_countertrend_loss_time: datetime | None = None
        self._short_cooldown_until: datetime | None = None  # Short obstinacy cooldown
        self._last_expansion_check_ts: int = 0  # Cache for expansion check
        
        # =========================================================================
        # 🔥 FIX #37 (Feb 05, 2026): OBSTINACY BLOCK COUNTER
        # When SHORT_OBSTINACY blocks 3+ times consecutively, invalidate edge
        # to allow direction change. Prevents stuck SHORT edge in bullish market.
        # =========================================================================
        self._obstinacy_block_count: int = 0
        
        # =========================================================================
        # 🔥 FIX #109 (Feb 09, 2026): TRANSITION_MODE
        # 
        # PROBLEMA: Después de BIAS_FLIP o DISPLACEMENT, el sistema aplica
        # demasiados penalties (volume, correlation, fade) que matan el edge.
        # 
        # SOLUCIÓN: Activar TRANSITION_MODE por 15 min post-transición:
        # - Durante este modo, el floor de penalties sube de 50% a 70%
        # - Permite capturar el edge de la transición sin sobre-filtrar
        # =========================================================================
        self._transition_mode_active: bool = False
        self._transition_mode_direction: str | None = None  # LONG/SHORT
        self._transition_mode_expires: datetime | None = None
        self._transition_mode_duration_minutes: int = 15
        
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
        
        🔄 REFACTORED Feb 04: Delegates to CooldownManager for unified cooldown handling.
        """
        if candles is None:
            candles = self._rejection_cooldown_candles
        
        # Calculate cooldown end time (candles * 60 seconds per 1m candle)
        cooldown_seconds = candles * 60
        market_time = self.market_data.get_market_time()
        
        # 🔄 REFACTORED Feb 04: Use CooldownManager
        self.cooldown_manager.activate_rejection_cooldown(
            current_time=market_time,
            reason=reason,
            cooldown_seconds=cooldown_seconds
        )
        
        # Sync back to legacy variable for backward compatibility
        sync_result = self.cooldown_manager.sync_to_app()
        self._rejection_cooldown_until = sync_result.get('rejection_cooldown_until')
        self._last_rejection_reason = reason
    
    def _is_in_rejection_cooldown(self) -> tuple[bool, str]:
        """
        Check if we're in a rejection cooldown period.
        
        🔄 REFACTORED Feb 04: Delegates to CooldownManager.
        """
        market_time = self.market_data.get_market_time()
        
        # Sync state from legacy variable to CooldownManager
        self.cooldown_manager.sync_from_app(
            cooldown_until=self._cooldown_until,
            rejection_cooldown_until=self._rejection_cooldown_until,
            short_cooldown_until=getattr(self, '_short_cooldown_until', None),
            consecutive_losses=self._consecutive_losses,
            kill_switch_active=getattr(self, '_kill_switch_active', False),
            tick_count=self._idle_eval_count
        )
        
        # Check via CooldownManager
        from src.application.live.cooldown_manager import CooldownType
        state = self.cooldown_manager._cooldowns.get(CooldownType.REJECTION)
        if state and state.active and not state.is_expired(market_time):
            remaining = state.remaining_seconds(market_time)
            return True, f"Rejection cooldown active ({remaining}s remaining): {self._last_rejection_reason}"
        
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
                    
                    # 🔥 FIX #105: Heartbeat mejorado con info de evaluación
                    edge_state = "N/A"
                    if hasattr(self, '_edge_manager') and self._edge_manager:
                        edge_state = self._edge_manager.state.value
                    
                    cooldown_info = ""
                    if hasattr(self.state_machine, '_cooldown_until') and self.state_machine._cooldown_until:
                        remaining = (self.state_machine._cooldown_until - tick_start).total_seconds()
                        if remaining > 0:
                            cooldown_info = f" | ⏳ cooldown={remaining:.0f}s"
                    
                    eval_count = getattr(self, '_idle_eval_count', 0)
                    logger.info(f"[BEAT] HEARTBEAT: tick #{tick_count}, state={self.state_machine.state.name}, pos={pos_status} | edge={edge_state} | evals={eval_count}{cooldown_info}")
                    
                    # 🔧 FIX #128 (Feb 09, 2026): Agregar info de precio y niveles clave cada 2 heartbeats
                    if tick_count % 80 == 0:  # Cada ~80 segundos
                        try:
                            candles = self.market_data.get_candles('1m')
                            if candles and len(candles) > 0:
                                price = candles[-1].get('close', 0)
                                swing_high = getattr(self, '_last_swing_high', 0)
                                swing_low = getattr(self, '_last_swing_low', 0)
                                swing_mid = (swing_high + swing_low) / 2 if swing_high > 0 and swing_low > 0 else price
                                pct_in_range = ((price - swing_low) / (swing_high - swing_low) * 100) if swing_high > swing_low else 50
                                
                                logger.info(
                                    f"📊 PRICE: ${price:.0f} | "
                                    f"SwingH=${swing_high:.0f} SwingL=${swing_low:.0f} | "
                                    f"Position in range: {pct_in_range:.0f}%"
                                )
                        except Exception:
                            pass
                    
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
                    
                    # 🔥 FIX (Feb 1, 2026): Force position sync after timeout
                    # Timeout can leave local state inconsistent with exchange
                    try:
                        if self.reconciler:
                            logger.info("🔄 Forcing position sync after timeout...")
                            await self.reconciler.reconcile()
                    except Exception as sync_err:
                        logger.error(f"Could not sync positions after timeout: {sync_err}")
                    
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
        # 🔥 FIX #28 (Feb 05, 2026): Pass regime_blocked to suppress redundant defensive mode
        price_change = abs(closes[-1] - closes[-2]) if len(closes) > 1 else 0
        last_candle = candles_1m[-1] if candles_1m else {}
        is_black_swan, bs_reason = self.black_swan_filter.check(
            price_change=price_change,
            atr=atr_1m,
            market_time=market_time,
            candle_high=float(last_candle.get('high', 0) or 0),
            candle_low=float(last_candle.get('low', 0) or 0),
            candle_open=float(last_candle.get('open', 0) or 0),
            candle_close=float(last_candle.get('close', 0) or 0),
            regime_blocked=self._regime_blocked,
            regime_block_reason=self._regime_block_reason
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
            # 🔥 FIX #105: Pasado a INFO cada 60 ticks (era DEBUG 30) para visibilidad
            if self._idle_eval_count % 60 == 0:
                logger.info(f"⏳ COOLDOWN: {cooldown_reason}")
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
        # 🔥 FIX #105: Log cada 60 ticks (era 120) para mejor observabilidad
        if self._idle_eval_count % 60 == 0:
            logger.info(f"📊 IDLE: Evaluated {self._idle_eval_count} ticks | Price: ${current_price:.2f} | ATR: {atr_1m:.2f}")
        
        # Reset trade tags for new evaluation
        self._current_trade_tags = {}
        
        # 🔥 FIX #28: Reset regime block state at start of each evaluation
        self._regime_blocked = False
        self._regime_block_reason = ""
        
        # =====================================================================
        # 🔥 FIX #32 (Feb 05, 2026): EDGE INVALIDATION - UNCONDITIONAL CHECK
        # 
        # CRITICAL: Edge expiration/invalidation MUST run on EVERY tick,
        # regardless of any filters. Previously this check was buried deep
        # in the evaluation chain, so if ANY filter returned early, the edge
        # would never be invalidated - causing stale edges to persist for
        # 10+ minutes while price moved against the thesis.
        #
        # Example from log: BOS SHORT at $72,869, price rose to $73,053 (+$184)
        # but edge remained ACTIVE because HTF filter returned before check.
        # =====================================================================
        if hasattr(self, '_edge_manager') and self._edge_manager:
            # Get ATR for invalidation buffer (use 1m as proxy, scale appropriately)
            # 🔥 FIX #77b: Pass swing_range for adaptive buffer
            swing_range = 0.0
            if hasattr(self, '_last_swing_high') and hasattr(self, '_last_swing_low'):
                if self._last_swing_high > 0 and self._last_swing_low > 0:
                    swing_range = self._last_swing_high - self._last_swing_low
            self._edge_manager.check_expiration(market_time)
            self._edge_manager.check_invalidation(current_price, atr_1m, swing_range)
        
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
            # 🔥 FIX #105: Log cada 30 ticks (era 100) para mejor observabilidad
            if self._idle_eval_count % 30 == 0:
                logger.info(f"⏸️ BLOCKED: {trade_reason}")
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
        
        # =====================================================================
        # 🔥 FIX #93 (Feb 09, 2026): VENTANA MÓVIL PARA EXTREMOS
        # =====================================================================
        # PROBLEMA ANTERIOR: _bias_high_since y _bias_low_since solo se 
        # reseteaban cada HORA. Esto causaba que el displacement se detectara
        # DESPUÉS del movimiento porque los extremos seguían creciendo.
        #
        # SOLUCIÓN: Usar ventana móvil de 10 velas de 1m (~10 min) para
        # calcular los extremos, en lugar de acumular desde el inicio de la hora.
        # =====================================================================
        candles_1m_for_extremes = self.market_data.get_candles('1m')
        if candles_1m_for_extremes and len(candles_1m_for_extremes) >= 10:
            # Usar últimas 10 velas para extremos (ventana móvil)
            recent_candles = candles_1m_for_extremes[-10:]
            rolling_high = max(c['high'] for c in recent_candles)
            rolling_low = min(c['low'] for c in recent_candles)
            
            # Solo actualizar si los nuevos extremos son más restrictivos
            # (esto evita que el rango se expanda infinitamente)
            if self._bias_high_since == 0:
                self._bias_high_since = rolling_high
                self._bias_low_since = rolling_low
            else:
                # Contraer gradualmente hacia la ventana móvil
                # Pero permitir que se expanda si el precio actual rompe
                self._bias_high_since = max(rolling_high, current_price)
                self._bias_low_since = min(rolling_low, current_price)
        else:
            # Fallback: comportamiento anterior si no hay suficientes velas
            if self._bias_high_since > 0:
                self._bias_high_since = max(self._bias_high_since, current_price)
                self._bias_low_since = min(self._bias_low_since, current_price)
            else:
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
        # 🌊 ADAPTIVE PARAMETERS - Using DirectionStateManager (Feb 04, 2026)
        # =====================================================================
        self.direction_state_mgr.update_tick_count(self._idle_eval_count)
        adaptive_params = self.direction_state_mgr.calculate_adaptive_params(
            atr_5m=atr_5m,
            atr_5m_avg=atr_5m_avg,
            current_htf_adx=self._htf_cache_adx if hasattr(self, '_htf_cache_adx') else 20.0
        )
        # Sync state with manager
        self._current_volatility_ratio = adaptive_params.volatility_ratio
        self._expansion_timeout_minutes = adaptive_params.expansion_timeout_minutes
        self._direction_lock_timeout_minutes = adaptive_params.direction_lock_timeout_minutes
        self._asymmetric_invalidation_mult = adaptive_params.asymmetric_invalidation_mult
        self._current_trade_tags.update(self.direction_state_mgr.trade_tags)
        
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
        # 🔥 DIRECTION_LOCK + EXPANSION_MODE State Management (Refactored Feb 04)
        # 
        # Delegado a DirectionStateManager para:
        # - Check timeouts
        # - Check asymmetric invalidation
        # - Sync state back to trading_app
        # =====================================================================
        
        # Sync current state TO handler
        self.direction_state_mgr.sync_from_app(
            direction_lock=self._direction_lock,
            direction_lock_reason=self._direction_lock_reason,
            direction_lock_expires_at=self._direction_lock_expires_at,
            direction_lock_initial_move=self._direction_lock_initial_move,
            expansion_mode=self._expansion_mode,
            expansion_direction=self._expansion_direction,
            expansion_start_time=self._expansion_start_time,
            expansion_initial_displacement=self._expansion_initial_displacement,
            bias_high_since=self._bias_high_since,
            bias_low_since=self._bias_low_since,
            forbidden_direction=self._forbidden_direction,
            expansion_timeout_minutes=self._expansion_timeout_minutes,
            asymmetric_invalidation_mult=self._asymmetric_invalidation_mult
        )
        
        # Check DIRECTION_LOCK timeout
        self.direction_state_mgr.check_direction_lock_timeout(datetime.now())
        
        # Check DIRECTION_LOCK asymmetric invalidation
        # FEB06: Ahora incluye time decay + momentum override
        _adx_for_lock = self._htf_cache_adx if hasattr(self, '_htf_cache_adx') else 20.0
        # Convertir volatility_ratio a percentile aproximado: ratio 1.5 → ~70%, ratio 0.7 → ~30%
        _vol_pct_for_lock = 50.0 + (self._current_volatility_ratio - 1.0) * 40.0
        _vol_pct_for_lock = max(0.0, min(100.0, _vol_pct_for_lock))
        
        self.direction_state_mgr.check_direction_lock_invalidation(
            drop_from_high=drop_from_high,
            rise_from_low=rise_from_low,
            atr_5m=atr_5m,
            adx=_adx_for_lock,
            vol_percentile=_vol_pct_for_lock
        )
        
        # Check EXPANSION_MODE timeout
        self.direction_state_mgr.check_expansion_timeout()
        
        # Check EXPANSION_MODE asymmetric invalidation
        self.direction_state_mgr.check_expansion_invalidation(
            drop_from_high=drop_from_high,
            rise_from_low=rise_from_low,
            atr_5m=atr_5m,
            atr_1m=atr_1m
        )
        
        # Sync state back FROM handler
        state_update = self.direction_state_mgr.sync_to_app()
        self._direction_lock = state_update['direction_lock']
        self._direction_lock_reason = state_update['direction_lock_reason']
        self._direction_lock_expires_at = state_update['direction_lock_expires_at']
        self._direction_lock_initial_move = state_update['direction_lock_initial_move']
        self._expansion_mode = state_update['expansion_mode']
        self._expansion_direction = state_update['expansion_direction']
        self._expansion_start_time = state_update['expansion_start_time']
        self._expansion_initial_displacement = state_update['expansion_initial_displacement']
        self._forbidden_direction = state_update['forbidden_direction']
        
        # Merge trade tags
        self._current_trade_tags.update(self.direction_state_mgr.trade_tags)

        
        # =====================================================================
        # 🔥 BIAS INVALIDATION - Refactored to use BiasInvalidationHandler
        # =====================================================================
        
        # Get ATR_1m for threshold calculation
        atr_1m_val = self._current_trade_tags.get('atr_1m', atr_5m / 2.5)
        self._current_trade_tags['atr_1m_for_bias'] = atr_1m_val
        
        # Get recent structure levels for confirmation
        # 🔥 FIX (Feb 04, 2026): Reduced lookback from 20 to 12 velas (1h instead of 100min)
        # This makes structure detection more responsive to recent price action
        SWING_LOOKBACK_5M = 12  # 12 velas de 5m = 1 hora
        MICRO_SWING_LOOKBACK_1M = 15  # 15 velas de 1m = 15 minutos
        
        try:
            candles_5m = self.market_data.get_candles('5m')
            if candles_5m and len(candles_5m) >= SWING_LOOKBACK_5M:
                recent_highs = [c['high'] for c in candles_5m[-SWING_LOOKBACK_5M:]]
                recent_lows = [c['low'] for c in candles_5m[-SWING_LOOKBACK_5M:]]
                swing_high_5m = max(recent_highs)
                swing_low_5m = min(recent_lows)
            else:
                swing_high_5m = self._bias_high_since
                swing_low_5m = self._bias_low_since
        except Exception:
            swing_high_5m = self._bias_high_since
            swing_low_5m = self._bias_low_since
        
        # 🔥 NEW (Feb 04, 2026): MICRO SWING with 1m candles for faster detection
        # This allows earlier structure break detection than 5m swings
        micro_swing_high = None
        micro_swing_low = None
        try:
            candles_1m = self.market_data.get_candles('1m')
            if candles_1m and len(candles_1m) >= MICRO_SWING_LOOKBACK_1M:
                micro_highs = [c['high'] for c in candles_1m[-MICRO_SWING_LOOKBACK_1M:]]
                micro_lows = [c['low'] for c in candles_1m[-MICRO_SWING_LOOKBACK_1M:]]
                micro_swing_high = max(micro_highs)
                micro_swing_low = min(micro_lows)
        except Exception:
            pass  # micro_swings remain None if error
        
        # 🔥 Store micro_swings for use by FastReversalDetector
        self._current_micro_swing_high = micro_swing_high
        self._current_micro_swing_low = micro_swing_low
        
        # Initialize tracking variables if needed
        if not hasattr(self, '_bias_invalidation_level'):
            self._bias_invalidation_level: float = 0.0
            self._bias_before_invalidation: str | None = None
        if not hasattr(self, '_forbidden_set_at_tick'):
            self._forbidden_set_at_tick: int = 0
        
        # Hysteresis check
        FORBIDDEN_MIN_DURATION_TICKS = 120
        ticks_since_forbidden = self._idle_eval_count - self._forbidden_set_at_tick
        can_clear_forbidden = ticks_since_forbidden >= FORBIDDEN_MIN_DURATION_TICKS
        
        # Check bias invalidation using handler
        # 🔥 FIX (Feb 04, 2026): Pass micro_swings for faster structure detection
        invalidation_result = self.bias_invalidation_handler.check_invalidation(
            bias=bias,
            current_price=current_price,
            bias_high_since=self._bias_high_since,
            bias_low_since=self._bias_low_since,
            atr_1m=atr_1m_val,
            swing_high_5m=swing_high_5m,
            swing_low_5m=swing_low_5m,
            micro_swing_high=micro_swing_high,  # 🔥 NEW: 1m micro structure
            micro_swing_low=micro_swing_low,    # 🔥 NEW: 1m micro structure
            expansion_mode=self._expansion_mode,
            expansion_direction=self._expansion_direction,
            direction_lock=self._direction_lock,
            expansion_initial_displacement=self._expansion_initial_displacement,
            direction_lock_initial_move=self._direction_lock_initial_move,
            tick_count=self._idle_eval_count,
            can_clear_forbidden=can_clear_forbidden,
            current_forbidden=self._forbidden_direction
        )
        
        # Apply invalidation result
        if invalidation_result.should_forbid:
            self._forbidden_direction = invalidation_result.forbidden_direction
            self._forbidden_set_at_tick = self._idle_eval_count
            # Set reclaim level
            if invalidation_result.forbidden_direction == 'LONG':
                self._bias_invalidation_level = self._bias_high_since - invalidation_result.threshold_used * 0.5
            else:
                self._bias_invalidation_level = self._bias_low_since + invalidation_result.threshold_used * 0.5
            self._bias_before_invalidation = invalidation_result.forbidden_direction
            self._current_trade_tags['reclaim_level'] = self._bias_invalidation_level
            
            # =====================================================================
            # 🔥 FIX #109 (Feb 09, 2026): Activar TRANSITION_MODE en BIAS_FLIP
            #
            # Cuando hay BIAS_FLIP, activamos TRANSITION_MODE por 15 minutos.
            # Durante este período, el floor de penalties sube de 50% a 70%
            # para capturar el edge de la transición sin sobre-filtrar.
            # 
            # 🔥 FIX #121 (Feb 09): THROTTLE - Solo log si realmente es NUEVO
            # =====================================================================
            new_direction = 'LONG' if invalidation_result.forbidden_direction == 'SHORT' else 'SHORT'
            was_already_active = self._transition_mode_active and self._transition_mode_direction == new_direction
            self._transition_mode_active = True
            self._transition_mode_direction = new_direction
            self._transition_mode_expires = datetime.now() + timedelta(
                minutes=self._transition_mode_duration_minutes
            )
            # 🔥 FIX #121: Solo log si no estaba activo o cambió dirección
            if not was_already_active:
                logger.info(
                    f"🚀 TRANSITION_MODE: {new_direction} for {self._transition_mode_duration_minutes}min | Penalty floor 70%"
                )
            
            # =====================================================================
            # 🔥 FIX #114 (Feb 09, 2026): TRANSITION_MODE CLEARS COOLDOWNS
            #
            # PROBLEMA (P4): Cooldown + Bias + Lock = PARÁLISIS
            # Cuando el mercado gira y tenemos BIAS_FLIP, el sistema debería
            # estar LISTO para entrar en la nueva dirección. Pero si hay cooldowns
            # activos de la dirección anterior, perdemos el edge de transición.
            #
            # SOLUCIÓN: Al activar TRANSITION_MODE, limpiar cooldowns no críticos.
            # - Mantener: CATASTROPHIC, DAILY_LIMIT
            # - Limpiar: POST_TRADE, COUNTERTREND, EDGE_CONTEXT, etc.
            # =====================================================================
            if hasattr(self, '_cooldown_manager') and self._cooldown_manager:
                from src.application.live.cooldown_manager import CooldownType
                cleared_cooldowns = []
                
                # List of cooldowns to clear during transition
                clearable_types = [
                    CooldownType.POST_TRADE,
                    CooldownType.COUNTERTREND,
                    CooldownType.EDGE_CONTEXT,
                    CooldownType.REVERSAL,
                ]
                
                for cd_type in clearable_types:
                    if self._cooldown_manager.is_cooldown_active(cd_type):
                        self._cooldown_manager._cooldowns[cd_type].active = False
                        cleared_cooldowns.append(cd_type.value)
                
                if cleared_cooldowns:
                    logger.info(
                        f"🔓 FIX #114: Cleared cooldowns for TRANSITION_MODE: {', '.join(cleared_cooldowns)}"
                    )
            
            # Save displacement memory if needed
            if invalidation_result.displacement_memory_saved:
                self._displacement_memory_direction = invalidation_result.displacement_memory_direction
                self._displacement_memory_amount = invalidation_result.displacement_memory_amount
                self._displacement_memory_expires_at = datetime.now() + timedelta(
                    minutes=self._displacement_memory_window_minutes
                )
        elif invalidation_result.forbidden_direction is None and self._forbidden_direction and can_clear_forbidden:
            # Clear forbidden after hysteresis
            self._forbidden_direction = None
        
        # Merge trade tags
        self._current_trade_tags.update(invalidation_result.to_tags())
        self._current_trade_tags['bias_invalidation_threshold'] = invalidation_result.threshold_used
        
        # =====================================================================
        # 🔥 BIAS RESTORATION (reversible invalidation)
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
                            bullish_count = sum(1 for i in range(len(last_3_closes)) 
                                                if last_3_closes[i] > last_3_opens[i])
                            momentum_ok = bullish_count >= 2
                        else:
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
        # 🔥 FIX (Feb 04, 2026): Subir threshold de 1.2 a 2.8 ATR
        # PROBLEMA: Con 1.2 ATR, displacement se detectaba en micromovimientos ($200-300)
        #           Esto activaba FAST PATH en ruido normal → 60% de trades eran FAST PATH
        #           FAST PATH tiene expectancy -0.012R (3x peor que Normal)
        # SOLUCIÓN: Threshold ~1.1% del precio (2.8 ATR con ATR_5m típico de $300)
        #           Solo movimientos REALES ($800+) activarán DISPLACEMENT
        #           El pipeline NORMAL volverá a funcionar la mayoría del tiempo
        BASE_DISPLACEMENT_ATR_MULT = 2.8  # ~1.1% del precio con ATR típico
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
        
        # Store displacement detection (will be passed to orchestrator)
        displacement_detected = False
        displacement_direction = None  # str | None
        
        # Calculate displacement from session prices
        if drop_from_high >= displacement_threshold:
            # Strong downward displacement detected
            # NOTE: _active_direction is set by orchestrator, not here
            displacement_detected = True
            displacement_direction = 'SHORT'
            self._displacement_bypass = True
            
            # 🔥 STRONG DISPLACEMENT → ACTIVATE EXPANSION_MODE + DIRECTION_LOCK
            if drop_from_high >= strong_displacement_threshold:
                self._strong_displacement = True
                
                # =========================================================
                # 🔥 FIX #9 (Feb 2, 2026): STALE DISPLACEMENT CHECK
                # 
                # PROBLEMA: Cuando EXPANSION_MODE hace timeout, el sistema
                # detecta el mismo displacement (precio sigue bajo) y RE-ACTIVA
                # el edge inmediatamente. Pero el momentum ya se agotó y el
                # precio puede subir → pérdida.
                #
                # SOLUCIÓN: Para SHORT, verificar que el precio NO esté cerca
                # del mínimo reciente (< 0.5 ATR). Si está cerca del mínimo,
                # el movimiento bajista ya se agotó → NO renovar edge.
                #
                # Lógica: distance_from_extreme = current_price - _bias_low_since
                # Si distance < 0.5 ATR → STALE (agotado)
                # =========================================================
                distance_from_low = current_price - self._bias_low_since
                stale_threshold = atr_5m * 0.5  # Dentro de 0.5 ATR del extremo = stale
                is_stale_displacement = distance_from_low < stale_threshold and distance_from_low >= 0
                
                # =========================================================
                # 🔥 FIX #10 (Feb 4, 2026): RANGE_BOUND FLOOR CHECK
                #
                # PROBLEMA: En RANGE_BOUND, el displacement hacia abajo
                # simplemente te lleva al FLOOR del rango, no es breakout.
                # Hacer SHORT en el floor del rango = atrapado.
                #
                # SOLUCIÓN: Si TrendPhase=RANGE_BOUND y distancia al low
                # es < 1.5 ATR → NO armar edge (estamos en el floor).
                # El rango necesita romperse primero.
                # =========================================================
                trend_phase_check = getattr(self, '_trend_phase', TrendPhase.RANGE_BOUND)
                is_range_bound = trend_phase_check == TrendPhase.RANGE_BOUND
                range_floor_threshold = atr_5m * 1.5  # Cerca del floor si < 1.5 ATR
                is_at_range_floor = is_range_bound and distance_from_low < range_floor_threshold
                
                if is_at_range_floor:
                    self._current_trade_tags['range_floor_block'] = True
                    self._current_trade_tags['range_floor_distance'] = distance_from_low
                    self._current_trade_tags['range_floor_threshold'] = range_floor_threshold
                    if self._idle_eval_count % 30 == 1 or not hasattr(self, '_last_range_floor_log'):
                        logger.warning(
                            f"🚫 RANGE FLOOR BLOCK SHORT: TrendPhase=RANGE_BOUND and price ${current_price:.0f} "
                            f"is only ${distance_from_low:.0f} from low ${self._bias_low_since:.0f} "
                            f"(< {range_floor_threshold:.0f} = 1.5 ATR) | "
                            f"Cannot SHORT at the floor of a range - wait for breakout"
                        )
                        self._last_range_floor_log = True
                    # Treat as stale for the purpose of blocking
                    is_stale_displacement = True
                
                if is_stale_displacement and not is_at_range_floor:
                    self._current_trade_tags['stale_displacement'] = True
                    self._current_trade_tags['stale_distance_from_extreme'] = distance_from_low
                    self._current_trade_tags['stale_threshold'] = stale_threshold
                    if self._idle_eval_count % 30 == 1 or not hasattr(self, '_last_stale_log_short'):
                        logger.warning(
                            f"⚠️ STALE DISPLACEMENT SHORT: Price ${current_price:.0f} is only ${distance_from_low:.0f} "
                            f"from low ${self._bias_low_since:.0f} (< {stale_threshold:.0f} ATR threshold) | "
                            f"Momentum exhausted → NOT arming edge"
                        )
                        self._last_stale_log_short = True
                
                # 🔥 EXPANSION_MODE ACTIVATION: Only activate if not already in expansion
                # or if this is a stronger displacement in the same direction
                # 🔥 FIX #9: Also check for stale displacement
                if (not self._expansion_mode or self._expansion_direction != 'SHORT') and not is_stale_displacement:
                    self._expansion_mode = True
                    self._expansion_direction = 'SHORT'
                    self._expansion_start_time = datetime.now()
                    self._expansion_initial_displacement = drop_from_high
                    self._last_stale_log_short = False  # Reset stale log flag
                    
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
                    # 🔥 FIX (Feb 4, 2026): Pass STALE info to block stale edges
                    # =========================================================
                    displacement_edge_armed = self._edge_manager.arm_displacement_edge(
                        current_price=current_price,
                        direction='SHORT',
                        displacement_amount=drop_from_high,
                        atr=atr_5m,
                        market_time=market_time,
                        initial_price=self._bias_high_since,
                        is_stale=is_stale_displacement,
                        stale_distance=distance_from_low
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
            # Strong upward displacement detected
            # NOTE: _active_direction is set by orchestrator, not here
            displacement_detected = True
            displacement_direction = 'LONG'
            self._displacement_bypass = True
            
            # 🔥 STRONG DISPLACEMENT → ACTIVATE EXPANSION_MODE + DIRECTION_LOCK
            if rise_from_low >= strong_displacement_threshold:
                self._strong_displacement = True
                
                # =========================================================
                # 🔥 FIX #9 (Feb 2, 2026): STALE DISPLACEMENT CHECK (LONG)
                # 
                # PROBLEMA: Para LONG, si el precio está muy cerca del
                # máximo reciente, el momentum alcista ya se agotó.
                #
                # Lógica: distance_from_extreme = _bias_high_since - current_price
                # Si distance < 0.5 ATR → STALE (agotado)
                # =========================================================
                distance_from_high = self._bias_high_since - current_price
                stale_threshold_long = atr_5m * 0.5  # Dentro de 0.5 ATR del extremo = stale
                is_stale_displacement_long = distance_from_high < stale_threshold_long and distance_from_high >= 0
                
                # =========================================================
                # 🔥 FIX #10 (Feb 4, 2026): RANGE_BOUND CEILING CHECK
                #
                # PROBLEMA: En RANGE_BOUND, el displacement hacia arriba
                # simplemente te lleva al CEILING del rango, no es breakout.
                # Hacer LONG en el ceiling del rango = atrapado.
                #
                # SOLUCIÓN: Si TrendPhase=RANGE_BOUND y distancia al high
                # es < 1.5 ATR → NO armar edge (estamos en el ceiling).
                # =========================================================
                trend_phase_check_long = getattr(self, '_trend_phase', TrendPhase.RANGE_BOUND)
                is_range_bound_long = trend_phase_check_long == TrendPhase.RANGE_BOUND
                range_ceiling_threshold = atr_5m * 1.5  # Cerca del ceiling si < 1.5 ATR
                is_at_range_ceiling = is_range_bound_long and distance_from_high < range_ceiling_threshold
                
                if is_at_range_ceiling:
                    self._current_trade_tags['range_ceiling_block'] = True
                    self._current_trade_tags['range_ceiling_distance'] = distance_from_high
                    self._current_trade_tags['range_ceiling_threshold'] = range_ceiling_threshold
                    if self._idle_eval_count % 30 == 1 or not hasattr(self, '_last_range_ceiling_log'):
                        logger.warning(
                            f"🚫 RANGE CEILING BLOCK LONG: TrendPhase=RANGE_BOUND and price ${current_price:.0f} "
                            f"is only ${distance_from_high:.0f} from high ${self._bias_high_since:.0f} "
                            f"(< {range_ceiling_threshold:.0f} = 1.5 ATR) | "
                            f"Cannot LONG at the ceiling of a range - wait for breakout"
                        )
                        self._last_range_ceiling_log = True
                    # Treat as stale for the purpose of blocking
                    is_stale_displacement_long = True
                
                if is_stale_displacement_long and not is_at_range_ceiling:
                    self._current_trade_tags['stale_displacement'] = True
                    self._current_trade_tags['stale_distance_from_extreme'] = distance_from_high
                    self._current_trade_tags['stale_threshold'] = stale_threshold_long
                    if self._idle_eval_count % 30 == 1 or not hasattr(self, '_last_stale_log_long'):
                        logger.warning(
                            f"⚠️ STALE DISPLACEMENT LONG: Price ${current_price:.0f} is only ${distance_from_high:.0f} "
                            f"from high ${self._bias_high_since:.0f} (< {stale_threshold_long:.0f} ATR threshold) | "
                            f"Momentum exhausted → NOT arming edge"
                        )
                        self._last_stale_log_long = True
                
                # 🔥 EXPANSION_MODE ACTIVATION: Only activate if not already in expansion
                # or if this is a stronger displacement in the same direction
                # 🔥 FIX #9: Also check for stale displacement
                if (not self._expansion_mode or self._expansion_direction != 'LONG') and not is_stale_displacement_long:
                    self._expansion_mode = True
                    self._expansion_direction = 'LONG'
                    self._expansion_start_time = datetime.now()
                    self._expansion_initial_displacement = rise_from_low
                    self._last_stale_log_long = False  # Reset stale log flag
                    
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
                    # 🔥 FIX (Feb 4, 2026): Pass STALE info to block stale edges
                    # =========================================================
                    displacement_edge_armed = self._edge_manager.arm_displacement_edge(
                        current_price=current_price,
                        direction='LONG',
                        displacement_amount=rise_from_low,
                        atr=atr_5m,
                        market_time=market_time,
                        initial_price=self._bias_low_since,
                        is_stale=is_stale_displacement_long,
                        stale_distance=distance_from_high
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
            displacement_detected = False
            displacement_direction = None
            
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
            
            # =====================================================================
            # 🔄 FAST REVERSAL DETECTION (Feb 2, 2026)
            # 
            # Check for single-candle reversals that override lagging indicators.
            # This solves the problem of: 5 bearish candles → 1 bullish reversal
            # where the system would stay SHORT due to bearish_score > bullish_score.
            #
            # When a strong reversal candle is detected:
            # - Override micro_momentum to new direction
            # - Log the reversal for debugging
            # =====================================================================
            try:
                # Get current bias for reversal detection
                current_bias_for_reversal = micro_momentum or self._structural_bias or 'NEUTRAL'
                
                if current_bias_for_reversal in ('LONG', 'SHORT'):
                    candles_for_reversal = self.market_data.get_candles('1m')
                    if candles_for_reversal and len(candles_for_reversal) >= 10:
                        # 🔥 FIX (Feb 04, 2026): Use micro_swings for faster reversal detection
                        # Prefer 1m micro_swing over 1H swing for faster response
                        # Fallback to 1H swing if micro_swing not available
                        _swing_high_for_reversal = (
                            getattr(self, '_current_micro_swing_high', None) or
                            (self._last_swing_high if self._last_swing_high > 0 else None)
                        )
                        _swing_low_for_reversal = (
                            getattr(self, '_current_micro_swing_low', None) or
                            (self._last_swing_low if self._last_swing_low > 0 else None)
                        )
                        
                        reversal_signal = self.fast_reversal_detector.check(
                            candles_1m=candles_for_reversal,
                            current_bias=current_bias_for_reversal,
                            atr_1m=atr_1m,
                            current_tick=self._idle_eval_count,
                            # 🔥 FIX: Use micro_swing (1m) instead of 1H swing for faster detection
                            swing_high=_swing_high_for_reversal,
                            swing_low=_swing_low_for_reversal
                        )
                        
                        if reversal_signal.detected and reversal_signal.new_direction:
                            # Override micro_momentum with reversal direction
                            previous_momentum = micro_momentum
                            micro_momentum = reversal_signal.new_direction
                            self._cached_micro_momentum = micro_momentum
                            
                            # Log the reversal
                            if self._idle_eval_count % 5 == 0:  # Reduce log spam
                                logger.warning(
                                    f"🔄 FAST REVERSAL DETECTED: {reversal_signal.signal_type} | "
                                    f"Previous={previous_momentum or 'None'} → New={reversal_signal.new_direction} | "
                                    f"Body={reversal_signal.body_atr:.2f}x ATR, Volume={reversal_signal.volume_mult:.1f}x | "
                                    f"Confidence={reversal_signal.confidence:.0%} | Reason: {reversal_signal.reason}"
                                )
                            
                            self._current_trade_tags['fast_reversal_detected'] = True
                            self._current_trade_tags['fast_reversal_type'] = reversal_signal.signal_type
                            self._current_trade_tags['fast_reversal_direction'] = reversal_signal.new_direction
                            self._current_trade_tags['fast_reversal_confidence'] = reversal_signal.confidence
                        
                        # Also check for immediate flip (larger move over multiple candles)
                        if not reversal_signal.detected:
                            should_flip, flip_direction, flip_reason = check_immediate_flip(
                                candles_1m=candles_for_reversal,
                                current_bias=current_bias_for_reversal,
                                atr_5m=atr_5m
                            )
                            
                            if should_flip and flip_direction:
                                micro_momentum = flip_direction
                                self._cached_micro_momentum = micro_momentum
                                
                                if self._idle_eval_count % 10 == 0:
                                    logger.warning(f"🔄 IMMEDIATE FLIP: {flip_reason}")
                                
                                self._current_trade_tags['immediate_flip'] = True
                                self._current_trade_tags['immediate_flip_direction'] = flip_direction
                                
            except Exception as e:
                logger.debug(f"Fast reversal detection error: {e}")
            
            # =====================================================================
            # 🎭 UNIFIED DIRECTION ORCHESTRATION (Feb 2, 2026)
            # 
            # Instead of scattered if/elif logic, use the central orchestrator
            # that consolidates ALL direction signals with clear priority:
            # EXPANSION > DISPLACEMENT > FAST_REVERSAL > MICRO > STRUCTURAL
            # =====================================================================
            
            # Collect fast reversal state
            fast_reversal_detected = self._current_trade_tags.get('fast_reversal_detected', False)
            fast_reversal_direction = self._current_trade_tags.get('fast_reversal_direction')
            fast_reversal_confidence = self._current_trade_tags.get('fast_reversal_confidence', 0.0)
            
            # Micro momentum strength
            micro_strength = self._current_trade_tags.get('micro_momentum_strength', 'WEAK')
        
        # =====================================================================
        # 🎭 UNIFIED DIRECTION ORCHESTRATION (Feb 2, 2026 - REFACTORED)
        # 
        # ALL direction signals pass through the orchestrator with clear priority:
        # EXPANSION(100) > EDGE(95) > DISPLACEMENT(90) > ANTICIPATION(85) > 
        # FAST_REVERSAL(80) > MICRO(70) > STRUCTURAL(60)
        #
        # This eliminates the 8+ scattered _active_direction assignments!
        # =====================================================================
        
        # Collect edge signals
        edge_active = False
        edge_direction_signal = None
        edge_type_signal = None
        edge_confidence = 1.0
        
        if self._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            if self._edge_manager.current_bos:
                edge_active = True
                edge_direction_signal = self._edge_manager.current_bos.direction
                edge_type_signal = self._edge_manager.current_bos.edge_type.value if self._edge_manager.current_bos.edge_type else None
        
        # Collect anticipation signals
        # 🔥 FIX #95 (Feb 09): Aceptar todos los tipos de anticipation
        anticipation_pending = False
        anticipation_direction_signal = None
        anticipation_signal = self._current_trade_tags.get('anticipation_signal')
        anticipation_confidence_val = 0.0
        valid_anticipation_signals = ['COMPRESSION_BREAKOUT', 'TREND_PULLBACK', 'FAILED_TEST', 'MOMENTUM_EXHAUSTION']
        if anticipation_signal in valid_anticipation_signals:
            anticipation_pending = True
            anticipation_direction_signal = self._current_trade_tags.get('anticipation_direction')
            anticipation_confidence_val = self._current_trade_tags.get('anticipation_confidence', 0.55)
        
        # Collect fast reversal (might have been detected in the else block)
        fast_reversal_detected = self._current_trade_tags.get('fast_reversal_detected', False)
        fast_reversal_direction = self._current_trade_tags.get('fast_reversal_direction')
        fast_reversal_confidence = self._current_trade_tags.get('fast_reversal_confidence', 0.0)
        
        # Get micro momentum (was calculated in else block, might be cached)
        micro_momentum = getattr(self, '_cached_micro_momentum', None)
        micro_strength = self._current_trade_tags.get('micro_momentum_strength', 'WEAK')
        
        # Run orchestration with ALL signals
        orch_result = self.direction_orchestrator.orchestrate(
            # Expansion mode signals (Priority 100 - HIGHEST)
            expansion_mode=self._expansion_mode,
            expansion_direction=self._expansion_direction,
            direction_lock=self._direction_lock,
            
            # Edge signals (Priority 95 - SOVEREIGN AUTHORITY)
            edge_active=edge_active,
            edge_direction=edge_direction_signal,
            edge_type=edge_type_signal,
            edge_confidence=edge_confidence,
            
            # Displacement signals (Priority 90)
            displacement_detected=displacement_detected,
            displacement_direction=displacement_direction,
            strong_displacement=self._strong_displacement,
            
            # Anticipation signals (Priority 85)
            anticipation_pending=anticipation_pending,
            anticipation_direction=anticipation_direction_signal,
            anticipation_confidence=anticipation_confidence_val,
            
            # Fast reversal signals (Priority 80)
            fast_reversal_detected=fast_reversal_detected,
            fast_reversal_direction=fast_reversal_direction,
            fast_reversal_confidence=fast_reversal_confidence,
            
            # Micro momentum signals (Priority 70)
            micro_momentum=micro_momentum,
            micro_momentum_strength=micro_strength,
            
            # Structural signals (Priority 60)
            structural_bias=self._structural_bias,
            
            # Veto signals
            forbidden_direction=self._forbidden_direction,
            
            # Context
            tick_count=self._idle_eval_count
        )
        
        # Apply orchestration result - SINGLE POINT OF ASSIGNMENT
        self._active_direction = orch_result.final_direction
        
        # 🔥 REMOVED (Feb 05, 2026): Sync with UnifiedTradingIntelligence
        # It was 100% dead code - synced but NEVER queried.
        # DirectionOrchestrator is the SINGLE SOURCE OF TRUTH.
        
        # Add orchestration tags for debugging
        self._current_trade_tags.update(orch_result.to_tags())
        
        if orch_result.conflict_detected and self._idle_eval_count % 30 == 1:
            logger.info(
                f"🎭 ORCHESTRATED: {orch_result.final_direction} via "
                f"{orch_result.winning_signal.source if orch_result.winning_signal else 'NONE'} | "
                f"Signals: {len(orch_result.all_signals)}"
            )
        
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
            # 🔥 FIX #122: Throttle 60 → 180 ticks
            if self._idle_eval_count % 180 == 1:
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
        # 🔥 REFACTORED (Feb 04, 2026): Regime score calculation extracted
        # to regime_handlers.py - reduces ~70 lines to 5
        # =====================================================================
        trend_phase = getattr(self, '_trend_phase', TrendPhase.RANGE_BOUND)
        regime_result = calculate_regime_score(
            effective_adx=effective_adx,
            trend_phase=trend_phase,
            volatility_ratio=self._current_volatility_ratio,
            has_displacement=self._displacement_bypass
        )
        
        # Apply result
        regime_score = regime_result.score
        trading_mode = regime_result.trading_mode
        self._current_trade_tags.update(regime_result.to_tags())
        
        # Log discrepancy if any
        log_regime_score_if_discrepancy(
            regime_result, effective_adx, 
            self._current_volatility_ratio, self._idle_eval_count
        )
        
        # =====================================================================
        # 🔥 REFACTORED (Feb 04, 2026): Intraday edge data extracted
        # to regime_handlers.py - reduces ~60 lines to 2
        # =====================================================================
        candles_1m = self.market_data.get_candles('1m')
        calculate_intraday_edge_data(candles_1m, self._current_trade_tags)
        
        # Store variables needed downstream
        trend_phase_str = regime_result.trend_phase_str
        adx_suggested_mode = regime_result.adx_suggested_mode
        
        # Log mode changes with full context (rate limited)
        # 🔥 FIX #122: Throttle 30 → 120 ticks
        if self._idle_eval_count % 120 == 1:
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
                    # 🔥 FIX #83: NO HARD BLOCK → PENALIZE
                    # Low volume = riskier, so reduce size instead of blocking
                    low_vol_penalty = max(0.3, volume_ratio / MIN_VOLUME_RATIO_RANGE)
                    self._current_trade_tags['low_volume_penalty'] = low_vol_penalty
                    # 🔥 FIX #122: Throttle 60 → 180 ticks
                    if self._idle_eval_count % 180 == 1:
                        logger.info(
                            f"⚠️ RANGE: Low volume | vol_ratio={volume_ratio:.0%} < {MIN_VOLUME_RATIO_RANGE:.0%} | "
                            f"PENALTY: size_mult={low_vol_penalty:.2f}"
                        )
                    # Continue with penalty instead of blocking
            
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
                    # 🔥 UNIFIED: Use has_momentum_override instead of scattered checks
                    if self.has_momentum_override:
                        logger.info(
                            f"🔥 RANGE HIGH BYPASS: pct={pct_60d:.0f}% (HIGH) but MOMENTUM_OVERRIDE active | "
                            f"Allowing LONG - potential ATH breakout"
                        )
                        self._current_trade_tags['range_high_bypass'] = True
                    else:
                        logger.debug(f"⛔ RANGE MODE: At {pct_60d:.0f}% (HIGH≥{EXTREME_HIGH_PCT:.0f}%) - LONG blocked, only SHORT")
                        return  # Can't long at the top in range (unless momentum override)
            elif pct_60d <= EXTREME_LOW_PCT:
                at_extreme = True
                extreme_type = 'LOW'  # Only LONG allowed here
                if bias == 'SHORT':
                    # 🔥 UNIFIED: Use has_momentum_override instead of scattered checks
                    if self.has_momentum_override:
                        logger.info(
                            f"🔥 RANGE LOW BYPASS: pct={pct_60d:.0f}% (LOW) but MOMENTUM_OVERRIDE active | "
                            f"Allowing SHORT - potential breakdown"
                        )
                        self._current_trade_tags['range_low_bypass'] = True
                    else:
                        logger.debug(f"⛔ RANGE MODE: At {pct_60d:.0f}% (LOW) - SHORT blocked, only LONG")
                        return  # Can't short at the bottom in range (unless momentum override)
            
            if not at_extreme:
                # =========================================================================
                # 🔥 UNIFIED BYPASS CHECK (Feb 04, 2026)
                # Replaces 50+ lines of scattered bypass logic with ONE call
                # =========================================================================
                should_bypass, bypass_reason = self._should_bypass_regime_filter('RANGE_MID')
                
                if should_bypass:
                    logger.info(
                        f"🔥 RANGE BYPASS: {bypass_reason} | pct={pct_60d:.0f}% ignored | "
                        f"ACTIVE_DIRECTION={self._active_direction} | "
                        f"{'EXPANSION=' + str(self._expansion_direction) + ' | ' if self._expansion_mode else ''}"
                        f"Range filter bypassed - continuing evaluation"
                    )
                    self._current_trade_tags['range_bypass_reason'] = bypass_reason
                    self._current_trade_tags['range_size_mult'] = 0.60  # Slightly reduced size for range-break
                    
                    # 🔥 FIX #160 (Feb 10): ARM EDGE ON RANGE BYPASS
                    # Si hay bypass (displacement/expansion), armar edge
                    bypass_direction = self._active_direction or self._expansion_direction
                    if bypass_direction:
                        macro_str = self._current_bias_strength.value if self._current_bias_strength else None
                        edge_armed = self._edge_manager.arm_consensus_edge(
                            direction=bypass_direction,
                            current_price=current_price,
                            atr=atr_1m,
                            market_time=market_time,
                            reason=f"RANGE_BYPASS ({bypass_reason})",
                            macro_bias_strength=macro_str,
                            has_momentum_override=self.has_momentum_override
                        )
                        if edge_armed:
                            self._current_trade_tags['range_edge_armed'] = True
                            self._current_trade_tags['edge_source'] = f'RANGE_BYPASS'
                            logger.info(
                                f"🎯 EDGE ARMED: RANGE_BYPASS {bypass_direction} | {bypass_reason}"
                            )
                    # Continue to trade
                else:
                    # =========================================================================
                    # 🔥 SIMPLIFIED RANGE_MID LOGIC (Feb 05, 2026)
                    # 
                    # PROBLEMA: EdgeScore con 7 componentes era sobre-ingeniería
                    # - Pesos arbitrarios sin validación
                    # - Demasiada complejidad = más bugs potenciales
                    #
                    # SOLUCIÓN SIMPLE (como un trader real):
                    # 1. Si structural == active → tradear (dos fuentes coinciden)
                    # 2. Si fast_reversal confirma structural → tradear temprano
                    # 3. Si no hay consenso → no tradear
                    #
                    # Size reducido siempre (40%) porque estamos en zona media
                    # =========================================================================
                    
                    # 🔥 UNIFIED: Use _get_effective_direction instead of manual checks
                    direction, source, confidence = self._get_effective_direction()
                    
                    self._current_trade_tags['range_mid_mode'] = True
                    self._current_trade_tags['range_mid_direction'] = direction
                    self._current_trade_tags['range_mid_source'] = source
                    self._current_trade_tags['range_mid_confidence'] = confidence
                    
                    # Need consensus or high confidence for RANGE_MID
                    if direction and (source == 'CONSENSUS' or confidence >= 0.6):
                        # 🔥 FIX #83: In RANGE mode, we WANT to short at HIGH / long at LOW
                        # Don't use price_confirms_direction which blocks this exact behavior
                        # Instead, trust the range extremes (percentile_60d)
                        price_confirms = True
                        price_change = 0.0
                        confirm_reason = f"RANGE: {direction} at pct={pct_60d:.0f}% (extremes trusted)"
                        
                        # 🔥 FIX #122: Throttle 120 → 300 ticks para reducir spam
                        if self._idle_eval_count % 300 == 1:
                            logger.info(
                                f"✅ RANGE MID: {source} | direction={direction} | conf={confidence:.0%} | "
                                f"price: {confirm_reason} | pct={pct_60d:.0f}% | Size=40% | TRADING"
                            )
                        self._current_trade_tags['range_size_mult'] = 0.40
                        self._current_trade_tags['range_mid_reason'] = source
                        
                        # 🔥 FIX #160 (Feb 10): ARM EDGE IN RANGE MODE
                        # Problema: RANGE nunca armaba edge → can_evaluate_entry() False → no trades
                        # Solución: Si hay CONSENSUS en RANGE, armar edge igual que en TREND
                        macro_str = self._current_bias_strength.value if self._current_bias_strength else None
                        edge_armed = self._edge_manager.arm_consensus_edge(
                            direction=direction,
                            current_price=current_price,
                            atr=atr_1m,
                            market_time=market_time,
                            reason=f"RANGE_CONSENSUS (pct={pct_60d:.0f}%, conf={confidence:.0%})",
                            macro_bias_strength=macro_str,
                            has_momentum_override=self.has_momentum_override
                        )
                        if edge_armed:
                            self._current_trade_tags['range_edge_armed'] = True
                            self._current_trade_tags['edge_source'] = f'RANGE_{source}'
                            logger.info(
                                f"🎯 EDGE ARMED: RANGE_{source} {direction} | pct={pct_60d:.0f}% | "
                                f"conf={confidence:.0%} | Size=40%"
                            )
                        # Continue to evaluate trade
                    else:
                        # No consensus - no trade
                        self._current_trade_tags['range_mid_reason'] = 'NO_CONSENSUS'
                        self._regime_blocked = True
                        self._regime_block_reason = f"RANGE_MID_NO_CONSENSUS (source={source}, conf={confidence:.0%})"
                        
                        # 🔥 FIX #122: Throttle 60 → 300 ticks
                        if self._idle_eval_count % 300 == 1:
                            logger.info(
                                f"📊 RANGE MID: NO CONSENSUS | source={source} | conf={confidence:.0%} | "
                                f"pct={pct_60d:.0f}% | NO TRADE"
                            )
                        return  # No trade - but not a hard deadlock
                            
            else:
                # At extreme in RANGE - apply RANGE rules with standard sizing
                self._current_trade_tags['range_mode_extreme'] = extreme_type
                
                # Simple sizing based on how extreme we are
                if extreme_type == 'LOW':
                    extremity = (EXTREME_LOW_PCT - pct_60d) / EXTREME_LOW_PCT  # 1.0 at 0%, 0.0 at 15%
                else:
                    extremity = (pct_60d - EXTREME_HIGH_PCT) / (100 - EXTREME_HIGH_PCT)  # 1.0 at 100%, 0.0 at 85%
                
                extremity = max(0.0, min(1.0, extremity))
                
                # Size: 35% base + up to 25% more based on extremity
                size_mult = 0.35 + extremity * 0.25  # Range: 35-60%
                self._current_trade_tags['range_size_mult'] = size_mult
                self._current_trade_tags['range_extremity'] = round(extremity, 2)
                
                # 🔥 FIX #160 (Feb 10): ARM EDGE IN RANGE EXTREME
                # Problema: RANGE EXTREME nunca armaba edge → no trades
                # En extremos, armar edge en dirección contraria (mean reversion)
                range_direction = 'SHORT' if extreme_type == 'HIGH' else 'LONG'
                macro_str = self._current_bias_strength.value if self._current_bias_strength else None
                edge_armed = self._edge_manager.arm_consensus_edge(
                    direction=range_direction,
                    current_price=current_price,
                    atr=atr_1m,
                    market_time=market_time,
                    reason=f"RANGE_EXTREME_{extreme_type} (pct={pct_60d:.0f}%, extremity={extremity:.0%})",
                    macro_bias_strength=macro_str,
                    has_momentum_override=self.has_momentum_override
                )
                if edge_armed:
                    self._current_trade_tags['range_edge_armed'] = True
                    self._current_trade_tags['edge_source'] = f'RANGE_EXTREME_{extreme_type}'
                    logger.info(
                        f"🎯 EDGE ARMED: RANGE_EXTREME_{extreme_type} {range_direction} | "
                        f"pct={pct_60d:.0f}% | extremity={extremity:.0%} | Size={size_mult:.0%}"
                    )
                
                if self._idle_eval_count % 60 == 1:
                    logger.info(
                        f"📊 RANGE EXTREME: {extreme_type} | pct={pct_60d:.0f}% | extremity={extremity:.0%} | "
                        f"Size={size_mult:.0%} | TRADING"
                    )
        
        # ===================================================================
        # ===================================================================
        # TRANSITION MODE (RegimeScore 0.35-0.50)
        # 🔥 REFACTORED Feb 04: Uses _get_effective_direction() instead of duplicated logic
        # ===================================================================
        elif trading_mode == 'TRANSITION':
            self._current_trade_tags['transition_mode'] = True
            
            # Gradual size based on regime_score
            # 🔥 FIX #33 (Feb 05): Use correct SIZE_MULT_TRANSITION → SIZE_MULT_TREND interpolation
            transition_progress = (regime_score - REGIME.RANGE_MAX) / (REGIME.TRANSITION_MAX - REGIME.RANGE_MAX)
            transition_progress = max(0.0, min(1.0, transition_progress))
            gradual_size = SIZING.SIZE_MULT_TRANSITION + (transition_progress * (SIZING.SIZE_MULT_TREND - SIZING.SIZE_MULT_TRANSITION))
            
            self._current_trade_tags['transition_size_mult'] = gradual_size
            self._current_trade_tags['transition_progress'] = transition_progress
            
            # 🔥 UNIFIED: Get direction from single source
            direction, source, confidence = self._get_effective_direction()
            self._current_trade_tags['transition_direction'] = direction
            self._current_trade_tags['transition_source'] = source
            self._current_trade_tags['transition_confidence'] = confidence
            
            # Need CONSENSUS or high confidence for TRANSITION
            if direction and (source == 'CONSENSUS' or confidence >= 0.6):
                # 🔥 FIX #79 (Feb 07): Skip price validation if EDGE already ACTIVE in same direction
                # Problem: EDGE_ACTIVE SHORT during trend, but _price_confirms_direction blocks
                # because price bounced 0.2% in last 5 candles. This missed $600 drop.
                # Solution: Once EDGE is ACTIVE, trust it - don't second-guess every tick
                edge_already_active = (
                    self._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE] and
                    self._edge_manager.current_bos is not None and
                    self._edge_manager.current_bos.direction == direction
                )
                
                if not edge_already_active:
                    price_confirms, price_change, confirm_reason = self._price_confirms_direction(direction)
                    
                    if not price_confirms:
                        # Rate-limit warning to reduce log spam
                        if self._idle_eval_count % 30 == 1:
                            logger.warning(f"⚠️ TRANSITION: {source} BLOCKED BY PRICE | {confirm_reason}")
                        self._current_trade_tags['transition_reason'] = 'PRICE_CONTRADICTED'
                        self._regime_blocked = True
                        self._regime_block_reason = f"PRICE_CONTRADICTS: {confirm_reason}"
                        return
                
                # 🔥 FIX #151 (Feb 10, 2026): Rate-limit TRANSITION log to 60s
                now = market_time
                last_trans_log = getattr(self, '_last_transition_log', None)
                last_trans_dir = getattr(self, '_last_transition_dir', None)
                should_log = (
                    last_trans_log is None or 
                    (now - last_trans_log).total_seconds() >= 60 or
                    last_trans_dir != direction  # Always log direction changes
                )
                if should_log:
                    logger.info(
                        f"✅ TRANSITION: {source} | dir={direction} | conf={confidence:.0%} | "
                        f"Score={regime_score:.2f} | Size={gradual_size:.0%} | TRADING"
                    )
                    self._last_transition_log = now
                    self._last_transition_dir = direction
                self._current_trade_tags['transition_reason'] = source
                
                # 🔥 FIX #35 (Feb 05): ARM EDGE for TRANSITION - handle all sources
                # Use arm_consensus_edge for all TRANSITION signals (simpler, direct to ACTIVE)
                # 🔥 FIX #36: Always TRY to arm - method handles state internally (including EXPIRED reset)
                # 🔥 FIX #78 (Feb 07): Pass macro bias for coherence check
                macro_str = self._current_bias_strength.value if self._current_bias_strength else None
                edge_armed = self._edge_manager.arm_consensus_edge(
                    direction=direction,
                    current_price=current_price,
                    atr=atr_1m,
                    market_time=market_time,
                    reason=f"TRANSITION_{source} (Score={regime_score:.2f}, conf={confidence:.0%})",
                    macro_bias_strength=macro_str,
                    has_momentum_override=self.has_momentum_override
                )
                if edge_armed:
                    self._current_trade_tags['consensus_edge_armed'] = True
                    self._current_trade_tags['edge_source'] = f'TRANSITION_{source}'
            else:
                # No consensus - no trade
                self._current_trade_tags['transition_reason'] = 'NO_CONSENSUS'
                self._regime_blocked = True
                self._regime_block_reason = f"TRANSITION_NO_CONSENSUS (source={source}, conf={confidence:.0%})"
                
                if self._idle_eval_count % 30 == 1:
                    logger.info(
                        f"📊 TRANSITION: NO CONSENSUS | source={source} | conf={confidence:.0%} | "
                        f"Score={regime_score:.2f} | NO TRADE"
                    )
                return
        
        # ===================================================================
        # TREND MODE (RegimeScore >= 0.50)
        # 🔥 REFACTORED Feb 04: Uses _get_effective_direction() instead of duplicated logic
        # ===================================================================
        elif trading_mode == 'TREND':
            self._current_trade_tags['trend_mode'] = True
            
            # 🔥 UNIFIED: Get direction from single source
            direction, source, confidence = self._get_effective_direction()
            self._current_trade_tags['trend_direction'] = direction
            self._current_trade_tags['trend_source'] = source
            self._current_trade_tags['trend_confidence'] = confidence
            
            # Check for displacement
            has_displacement = self._displacement_bypass or self._expansion_mode
            self._current_trade_tags['trend_displacement'] = has_displacement
            
            # In TREND, more permissive - accept momentum alone
            if direction and confidence >= 0.5:
                # 🔥 FIX #52 (Feb 05): Validate price for ALL sources except DISPLACEMENT/EXPANSION
                # Problem: CONSENSUS check passed once, edge armed, edge expired, edge re-armed
                # from EDGE_ACTIVE source without price check → kept trying SHORT during $1000 rally
                # Solution: Always check price direction before arming/re-arming edge
                # 🔥 FIX (Feb 06): ALSO skip price check if EDGE is already ACTIVE
                # The edge already validated price when it was armed - don't second-guess
                edge_already_active = (
                    self._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE] and
                    self._edge_manager.current_bos is not None and
                    self._edge_manager.current_bos.direction == direction
                )
                
                if source not in ('DISPLACEMENT', 'EXPANSION_LOCK', 'DIRECTION_LOCK') and not edge_already_active:
                    price_confirms, price_change, confirm_reason = self._price_confirms_direction(direction)
                    
                    if not price_confirms:
                        # 🔥 FIX #122: Throttle 60 → 120 ticks
                        if self._idle_eval_count % 120 == 1:
                            logger.warning(f"⚠️ TREND: {source} BLOCKED BY PRICE | {confirm_reason}")
                        self._current_trade_tags['trend_reason'] = 'PRICE_CONTRADICTED'
                        return
                
                # Size based on ADX strength
                adx_strength = min(1.0, (effective_adx - ADX.TREND_MIN) / 30)
                trend_size = SIZING.TREND_BASE + adx_strength * SIZING.TREND_ADX_BONUS
                
                # 🔥 FIX #122: Throttle 60 → 180 ticks
                if self._idle_eval_count % 180 == 1:
                    logger.info(
                        f"✅ TREND: {source} | dir={direction} | conf={confidence:.0%} | "
                        f"ADX={effective_adx:.1f} | Size={trend_size:.0%} | TRADING"
                    )
                self._current_trade_tags['trend_size_mult'] = trend_size
                self._current_trade_tags['trend_reason'] = source
                
                # 🔥 FIX #34 (Feb 05): ARM EDGE for any source in TREND mode
                # All sources (CONSENSUS/DISPLACEMENT/MOMENTUM) can arm edge via arm_consensus_edge
                # arm_consensus_edge transitions DIRECTLY to EDGE_ACTIVE (no waiting for pullback)
                # 🔥 FIX #36: Always TRY to arm - method handles state internally (including EXPIRED reset)
                # 🔥 FIX #78 (Feb 07): Pass macro bias for coherence check
                macro_str = self._current_bias_strength.value if self._current_bias_strength else None
                edge_armed = self._edge_manager.arm_consensus_edge(
                    direction=direction,
                    current_price=current_price,
                    atr=atr_1m,
                    market_time=market_time,
                    reason=f"TREND_{source} (ADX={effective_adx:.1f}, conf={confidence:.0%})",
                    macro_bias_strength=macro_str,
                    has_momentum_override=self.has_momentum_override
                )
                if edge_armed:
                    self._current_trade_tags['consensus_edge_armed'] = True
                    self._current_trade_tags['edge_source'] = f'TREND_{source}'
                    logger.info(
                        f"🎯 EDGE ARMED: TREND_{source} {direction} | ADX={effective_adx:.1f} | "
                        f"conf={confidence:.0%} | DIRECT TO ACTIVE"
                    )
            else:
                # Waiting for edge
                self._current_trade_tags['trend_reason'] = 'WAITING_EDGE'
                
                # 🔥 FIX #122: Throttle 60 → 180 ticks
                if self._idle_eval_count % 180 == 1:
                    logger.info(
                        f"📊 TREND: WAITING EDGE | source={source} | conf={confidence:.0%} | "
                        f"ADX={effective_adx:.1f} | Need BOS/breakout"
                    )
                # DON'T return - let EdgeStateManager try to find edge
        
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
        
        # =========================================================================
        # 🔥 FIX #58 (Feb 06, 2026): Reducir swing lookback de 5→3
        # 
        # ANTES: lookback=5 en 1H = ~5 horas de delay para detectar swing
        # AHORA: lookback=3 en 1H = ~3 horas (40% más rápido)
        # 
        # Esto acelera la detección de swings y reduce latencia en BOS detection.
        # El tradeoff es swings menos "robustos" pero más reactivos.
        # =========================================================================
        SWING_LOOKBACK_1H = 3  # Reducido de 5 para detección más temprana
        
        from src.shared.indicators import detect_swing_high, detect_swing_low
        
        swing_high_detected, swing_high_price, _ = detect_swing_high(highs_1h, lows_1h, closes_1h, lookback=SWING_LOOKBACK_1H)
        swing_low_detected, swing_low_price, _ = detect_swing_low(highs_1h, lows_1h, closes_1h, lookback=SWING_LOOKBACK_1H)
        
        # Actualizar swings si se detectaron nuevos
        if swing_high_detected and swing_high_price > 0:
            if swing_high_price != self._last_swing_high:
                self._prev_swing_high = self._last_swing_high
                self._last_swing_high = swing_high_price
                # 🔧 FIX #128: Log nuevo swing high (informativo para detección de edge)
                logger.info(f"📍 NEW SWING HIGH: ${swing_high_price:.0f} (prev: ${self._prev_swing_high:.0f})")
        
        if swing_low_detected and swing_low_price > 0:
            if swing_low_price != self._last_swing_low:
                self._prev_swing_low = self._last_swing_low
                self._last_swing_low = swing_low_price
                # 🔧 FIX #128: Log nuevo swing low (informativo para detección de edge)
                logger.info(f"📍 NEW SWING LOW: ${swing_low_price:.0f} (prev: ${self._prev_swing_low:.0f})")
        
        # Inicializar swings si aún no están (usar highs/lows recientes)
        if self._last_swing_high == 0:
            self._last_swing_high = np.max(highs_1h[-10:]) if len(highs_1h) >= 10 else current_price
            self._prev_swing_high = np.max(highs_1h[-20:-10]) if len(highs_1h) >= 20 else self._last_swing_high
        if self._last_swing_low == 0:
            self._last_swing_low = np.min(lows_1h[-10:]) if len(lows_1h) >= 10 else current_price
            self._prev_swing_low = np.min(lows_1h[-20:-10]) if len(lows_1h) >= 20 else self._last_swing_low
        
        # 🔥 FIX #32 (Feb 05, 2026): Edge invalidation MOVED to top of _handle_idle_state
        # The check below is now REDUNDANT - the early check runs on EVERY tick unconditionally.
        # Keeping this as secondary check with ATR 1h (larger buffer, more permissive) 
        # in case the early check with ATR 1m missed something due to execution order.
        # This secondary check only runs if we get past all filters, so it's "belt and suspenders".
        # 🔥 FIX #77b: Pass swing_range for adaptive buffer
        swing_range_1h = self._last_swing_high - self._last_swing_low if (self._last_swing_high > 0 and self._last_swing_low > 0) else 0.0
        self._edge_manager.check_expiration(market_time)
        self._edge_manager.check_invalidation(current_price, atr_1h, swing_range_1h)
        
        # NOTE: EdgeType already imported at module level (line 220)
        # Do NOT import locally - causes UnboundLocalError at earlier uses
        
        # =====================================================================
        # 🌊 FIX (Jan 30, 2026): Update DISPLACEMENT entry window
        # 
        # Si hay DISPLACEMENT edge activo, actualizar la ventana de entrada
        # y matar el edge si está demasiado tarde (NO PERSEGUIR)
        # 
        # 🔥 FIX #159b: El cooldown ahora se activa automáticamente en _expire_edge()
        # No es necesario llamar set_post_displacement_cooldown() aquí.
        # =====================================================================
        if self._edge_manager.current_bos and self._edge_manager.current_bos.edge_type == EdgeType.DISPLACEMENT:
            should_kill, kill_reason = self._edge_manager.update_displacement_entry_window(market_time)
            if should_kill:
                logger.warning(f"🚫 {kill_reason}")
                self._edge_manager._expire_edge(kill_reason)  # Cooldown auto-activado
                self._current_trade_tags['displacement_killed_late'] = True
                self._current_trade_tags['post_displacement_cooldown'] = True
        
        # =====================================================================
        # 🔥 CRITICAL FIX (Jan 29, 2026): ANTICIPATION/EDGE NOW HANDLED BY ORCHESTRATOR
        # 
        # The DirectionOrchestrator now handles all direction signal priority:
        # EXPANSION(100) > EDGE(95) > DISPLACEMENT(90) > ANTICIPATION(85) > etc.
        #
        # This block is SIMPLIFIED to only handle edge INVALIDATION logic.
        # Direction overrides are NO LONGER DONE HERE - orchestrator decides.
        # =====================================================================
        anticipation_signal = self._current_trade_tags.get('anticipation_signal')
        anticipation_direction = self._current_trade_tags.get('anticipation_direction')
        # 🔥 FIX #95 (Feb 09): Aceptar todos los tipos de anticipation
        valid_anticipation_signals = ['COMPRESSION_BREAKOUT', 'TREND_PULLBACK', 'FAILED_TEST', 'MOMENTUM_EXHAUSTION']
        has_anticipation_breakout = (anticipation_signal in valid_anticipation_signals and anticipation_direction)
        
        # NOTE: Anticipation direction override REMOVED - orchestrator handles this
        # The orchestrator already set _active_direction with ANTICIPATION priority 85
        
        # =====================================================================
        # 🔥 EDGE INVALIDATION LOGIC (Jan 29, 2026)
        # 
        # When there's an edge active but direction MISMATCHES with orchestrated direction:
        # - BREAKOUT/DISPLACEMENT edges: DO NOT INVALIDATE (they have sovereign authority
        #   in the orchestrator, so mismatch means orchestrator chose a higher priority signal)
        # - TREND/RANGE edges: INVALIDATE if direction mismatch
        # - ANTICIPATION PENDING: Do not invalidate (new breakout will replace)
        # =====================================================================
        if self._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            edge_direction = None
            edge_type = None
            if self._edge_manager.current_bos:
                edge_direction = self._edge_manager.current_bos.direction
                edge_type = self._edge_manager.current_bos.edge_type
            
            if edge_direction and self._active_direction:
                if edge_direction != self._active_direction:
                    # BREAKOUT/DISPLACEMENT: The orchestrator already gave them priority 95.
                    # If we reached here with mismatch, it means EXPANSION_MODE (100) overrode.
                    # Log but do NOT invalidate - expansion mode should still use the edge.
                    if edge_type in [EdgeType.BREAKOUT, EdgeType.DISPLACEMENT]:
                        # 🔥 FIX #122: Throttle 60 → 120 ticks
                        if self._idle_eval_count % 120 == 1:
                            logger.debug(
                                f"📊 Edge-Direction divergence: Edge={edge_direction} vs "
                                f"Orchestrated={self._active_direction} | Edge type={edge_type.value} | "
                                f"Not invalidating - higher priority signal active"
                            )
                        # NOTE: Direction override REMOVED - orchestrator already decided
                    # ANTICIPATION PENDING: Don't invalidate
                    elif has_anticipation_breakout:
                        # 🔥 FIX #122: Throttle 60 → 120 ticks
                        if self._idle_eval_count % 120 == 1:
                            logger.debug(
                                f"🎯 ANTICIPATION BREAKOUT PENDING: Skipping edge invalidation | "
                                f"Old edge will be replaced by BREAKOUT {anticipation_direction}"
                            )
                    else:
                        # TREND/RANGE: invalidate if there's mismatch
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
            # 🔥 FIX #30 (Feb 05): QUICK CONFIRMATION MODE
            # 🔥 FIX #95 (Feb 09): Expandir a TODOS los tipos de anticipation
            # 
            # ANTICIPATION casi siempre acierta - necesita MENOS confirmación.
            # Nueva lógica:
            # 1. Si ANTICIPATION + micro_swing roto en esa dirección → entrada rápida
            # 2. Si solo ANTICIPATION conf≥60% → edge tradicional
            # =====================================================================
            anticipation_signal = self._current_trade_tags.get('anticipation_signal')
            # 🔥 FIX #95: Aceptar TODOS los tipos válidos de anticipation
            valid_anticipation_signals = ['COMPRESSION_BREAKOUT', 'TREND_PULLBACK', 'FAILED_TEST', 'MOMENTUM_EXHAUSTION']
            if anticipation_signal in valid_anticipation_signals:
                anticipation_direction = self._current_trade_tags.get('anticipation_direction', 'LONG')
                anticipation_bb_pct = self._current_trade_tags.get('anticipation_bb_percentile', 50.0)
                anticipation_vol_ratio = self._current_trade_tags.get('anticipation_volume_ratio', 1.0)
                anticipation_confidence = self._current_trade_tags.get('anticipation_confidence', 0.55)
                structure_level = self._current_trade_tags.get('anticipation_structure_level', 0.0)
                
                # =========================================================
                # 🔥 QUICK CONFIRMATION: Micro swing break = entrada rápida
                # =========================================================
                micro_high = getattr(self, '_current_micro_swing_high', None)
                micro_low = getattr(self, '_current_micro_swing_low', None)
                
                quick_confirmed = False
                if anticipation_direction == 'LONG' and micro_high is not None:
                    # LONG: si precio rompe micro_swing_high → confirmación rápida
                    if current_price > micro_high:
                        quick_confirmed = True
                        logger.info(
                            f"⚡ ANTICIPATION QUICK CONFIRM: LONG | price ${current_price:.0f} > "
                            f"micro_high ${micro_high:.0f} | conf={anticipation_confidence:.0%}"
                        )
                elif anticipation_direction == 'SHORT' and micro_low is not None:
                    # SHORT: si precio rompe micro_swing_low → confirmación rápida
                    if current_price < micro_low:
                        quick_confirmed = True
                        logger.info(
                            f"⚡ ANTICIPATION QUICK CONFIRM: SHORT | price ${current_price:.0f} < "
                            f"micro_low ${micro_low:.0f} | conf={anticipation_confidence:.0%}"
                        )
                
                if quick_confirmed and anticipation_confidence >= 0.50:
                    # QUICK PATH: Micro break + ANTICIPATION = entrar ahora
                    self._current_trade_tags['anticipation_quick_confirm'] = True
                    self._current_trade_tags['anticipation_entry'] = True
                    self._current_trade_tags['anticipation_probe_size'] = 0.35  # 35% size
                    self._active_direction = anticipation_direction
                    logger.info(
                        f"🚀 ANTICIPATION QUICK ENTRY ARMED | {anticipation_direction} | "
                        f"Confidence: {anticipation_confidence:.0%} | Quick confirm via micro break"
                    )
                    # Skip edge arming - go directly to entry pipeline
                elif anticipation_confidence >= 0.50:  # 🔥 FIX #97 (Feb 09): Bajado de 0.60 a 0.50
                    # STANDARD PATH: Edge tradicional
                    breakout_armed = self._edge_manager.check_for_breakout_edge(
                        current_price=current_price,
                        bb_width_percentile=anticipation_bb_pct,
                        volume_ratio=anticipation_vol_ratio,
                        direction=anticipation_direction,
                        atr=atr_1h,
                        market_time=market_time,
                        structure_level=structure_level
                    )
                    
                    if breakout_armed:
                        self._current_trade_tags['breakout_edge_armed'] = True
                        self._current_trade_tags['edge_type'] = 'BREAKOUT'
                        self._current_trade_tags['breakout_structure_level'] = structure_level
                        logger.info(
                            f"🔥 BREAKOUT EDGE ARMED from ANTICIPATION | "
                            f"Direction: {anticipation_direction} | conf={anticipation_confidence:.0%} | "
                            f"Structure: ${structure_level:.0f}"
                        )
            
            # =====================================================================
            # 🔥 NEW (Feb 04, 2026): RANGE edge more flexible
            # RANGE edge can activate when:
            #   1. trading_mode == 'RANGE' (classic)
            #   2. OR ADX < RANGE_EDGE_MAX (25) with extreme location
            # This allows mean-reversion in "soft trend" conditions
            # =====================================================================
            from src.config.trading_config import ADXThresholds
            
            # Check if we should try RANGE edge
            try_range_edge = trading_mode == 'RANGE'
            range_edge = False  # Initialize to avoid NameError
            
            # 🔥 NEW: Also try RANGE edge in TREND/TRANSITION if ADX is soft
            if not try_range_edge and effective_adx < ADXThresholds.RANGE_EDGE_MAX:
                percentile_check = self._location_context.percentile_60d if self._location_context else 50.0
                # Only if at extreme location (≤15% or ≥85%)
                if percentile_check <= RangeEdgeCriteria.EXTREME_LOW_PCT or percentile_check >= RangeEdgeCriteria.EXTREME_HIGH_PCT:
                    try_range_edge = True
                    logger.info(
                        f"📊 RANGE EDGE OVERRIDE: ADX={effective_adx:.1f}<{ADXThresholds.RANGE_EDGE_MAX} "
                        f"+ extreme pct={percentile_check:.0f}% → trying RANGE edge in {trading_mode} mode"
                    )
            
            if try_range_edge:
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
            
            # TREND/TRANSITION: Buscar BOS clásico (only if no RANGE edge found)
            if not try_range_edge or not range_edge:
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
        # NOTA (Feb 2, 2026): Esta lógica fue MIGRADA al DirectionOrchestrator.
        # El edge ahora tiene Priority 95 (SOVEREIGN AUTHORITY) en el orchestrator.
        # Este bloque solo sincroniza la variable `bias` local con la decisión
        # ya tomada por el orchestrator - NO modifica _active_direction aquí.
        # =====================================================================
        if self._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            edge_bos = self._edge_manager.current_bos
            if edge_bos and edge_bos.direction:
                edge_direction = edge_bos.direction
                
                # Sincronizar bias local (para este scope) con edge direction
                # NOTE: _active_direction ya fue seteado por el orchestrator
                if bias != edge_direction and edge_direction == self._active_direction:
                    old_bias = bias
                    bias = edge_direction
                    self.current_bias = bias
                    # NOTE: _active_direction override REMOVED - orchestrator handles this
                    
                    if self._idle_eval_count % 60 == 1:
                        logger.debug(
                            f"📊 Syncing local bias with orchestrated direction: {old_bias}→{edge_direction}"
                        )
        
        # 🔧 FIX #128 (Feb 09, 2026): Actualizar razón de NO_EDGE para logging informativo
        if self._edge_manager.state == EdgeState.NO_EDGE:
            # Determinar razón basada en el contexto actual
            percentile_60d = self._current_trade_tags.get('percentile_60d', 50.0)
            regime = self._current_trade_tags.get('regime_mode', self._htf_cache_regime_mode)
            anticipation = self._current_trade_tags.get('anticipation_signal')
            
            if regime == 'RANGE':
                if 15 < percentile_60d < 85:
                    reason = f"RANGE pero pct={percentile_60d:.0f}% no extremo (need <15 o >85)"
                else:
                    reason = "RANGE extremo pero sin sweep+rejection"
            elif anticipation:
                reason = f"ANTICIPATION {anticipation} sin confirmación micro-swing"
            else:
                reason = "Sin BOS/displacement detectado"
            
            self._edge_manager._no_edge_reason = reason
        
        # 5. Si NO hay edge activo → skip evaluación (el core del cambio)
        # PERO: Permitir modo PROBING para diagnóstico (0.25x size)
        # 🔥 FIX (Jan 28, 2026): PROBE mode puede operar sin edge activo
        #    ProbeValidator valida rentabilidad mínima
        # 🔥 FIX (Jan 28): PROBE mode eliminado en RANGE
        # 🔥 FIX #160 (Feb 10): RANGE ahora arma edges con CONSENSUS/EXTREME/BYPASS
        #    Si llegamos aquí con NO_EDGE, significa que no hubo CONSENSUS ni condición válida
        
        if not self._edge_manager.can_evaluate_entry():
            block_reason = self._edge_manager.get_block_reason()
            
            # 🔧 FIX #128 (Feb 09, 2026): Log diagnóstico informativo cada minuto
            if self._idle_eval_count % 60 == 1:
                # Recopilar información diagnóstica
                diag_parts = []
                
                # Modo de trading
                regime = self._current_trade_tags.get('regime_mode', self._htf_cache_regime_mode)
                diag_parts.append(f"mode={regime}")
                
                # Percentil de precio (ubicación)
                percentile = self._current_trade_tags.get('percentile_60d', 50.0)
                diag_parts.append(f"pct={percentile:.0f}%")
                
                # ADX (tendencia)
                adx_eff = self._current_trade_tags.get('adx_effective', 20.0)
                diag_parts.append(f"ADX={adx_eff:.0f}")
                
                # Anticipation si hay
                anticipation = self._current_trade_tags.get('anticipation_signal')
                if anticipation:
                    ant_dir = self._current_trade_tags.get('anticipation_direction', '?')
                    ant_conf = self._current_trade_tags.get('anticipation_confidence', 0)
                    diag_parts.append(f"ANT={anticipation}_{ant_dir}@{ant_conf:.0%}")
                
                # BB width (compresión)
                bb_pct = self._current_trade_tags.get('bb_width_percentile', 50.0)
                if bb_pct <= 10:
                    diag_parts.append(f"⚡SQUEEZE={bb_pct:.0f}%")
                
                # Bias
                bias = self._active_direction or 'NONE'
                diag_parts.append(f"bias={bias}")
                
                diag_str = " | ".join(diag_parts)
                logger.info(f"⏸️ {block_reason} | {diag_str}")
                
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
                # 🔥 FIX #122: Throttle 60 → 180 ticks
                if self._idle_eval_count % 180 == 1:
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
            
            # 🔥 FIX #68: REMOVED FIX #39b - Was causing log spam conflict with BIAS FLIP
            # The forbidden_direction can be set by:
            # 1. BIAS FLIP (bias_invalidation) → has its own reclaim logic
            # 2. Location extreme zone → should only be cleared by Location
            # 
            # Problem: FIX #39b was clearing ALL forbids based on Location,
            # including those set by BIAS FLIP, causing infinite spam:
            # - BIAS FLIP sets forbidden='SHORT' every tick
            # - FIX #39b clears it every tick (not in extreme_low)
            # - Log spams "SHORT ALLOWED AGAIN" on every tick
            #
            # Solution: Let each subsystem manage its own forbidden state.
            # BIAS FLIP already has reclaim logic in bias_invalidation_handler.
            # Location filter already applies penalties without needing this clear.
            pass  # Intentionally empty - each subsystem handles its own state
            
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
            # 🔥 FIX #70d: Store is_bullish_structure for CT block bypass
            self._is_bullish_structure = bias_result.is_bullish_structure
        else:
            self._current_bias_strength = BiasStrength.NEUTRAL
            self._is_bullish_structure = None
        
        # 🔥 FIX: Ensure _current_bias_strength is not None before accessing .value
        if self._current_bias_strength is None:
            self._current_bias_strength = BiasStrength.NEUTRAL
        self._current_trade_tags['bias_strength'] = self._current_bias_strength.value
        
        # =========================================================================
        # 🔥 FIX #76 (Feb 07, 2026): Calculate WORKING BIAS from MACRO BIAS
        # 
        # WorkingBias tells us what direction to PREFER, NOT what to BLOCK.
        # - BEAR_STRONG macro → SHORT_PREF (prefer shorts, allow longs with edge)
        # - BULL_STRONG macro → LONG_PREF (prefer longs, allow shorts with edge)
        # - NEUTRAL macro → BOTH (trade whatever triggers)
        # =========================================================================
        self._working_bias = BiasStrengthConfig.get_working_bias(self._current_bias_strength)
        self._current_trade_tags['working_bias'] = self._working_bias.value
        
        logger.debug(
            f"📊 BIAS: Macro={self._current_bias_strength.value} → Working={self._working_bias.value} | "
            f"is_bullish_structure={self._is_bullish_structure}"
        )
        
        # =========================================================================
        # 🔥 FIX #83 (Feb 07, 2026): 3-LAYER REGIME FUSION
        # 
        # CAPA 1 - MACRO: EMA50/200, +DI/-DI, EMA50 slope → macro_score [-2.5, +2.5]
        # CAPA 2 - MICRO: direction, confidence, displacement, structure
        # CAPA 3 - FUSIÓN: alignment × peso → size_mult, rr_min, allowed
        # 
        # PRINCIPIO: No bloquear → hacer caro equivocarte.
        # ÚNICO BLOQUEO: macro_strength > 2.0 AND contra AND sin displacement
        # =========================================================================
        
        # Calcular indicadores para MACRO
        ema50_1h = float(df_1h['close'].ewm(span=50, adjust=False).mean().iloc[-1])
        ema200_1h = float(df_1h['close'].ewm(span=200, adjust=False).mean().iloc[-1])
        
        # Obtener +DI/-DI del 1h
        try:
            _, _, plus_di_arr, minus_di_arr = calculate_adx_with_di(
                df_1h['high'].to_numpy(),
                df_1h['low'].to_numpy(),
                df_1h['close'].to_numpy(),
                14
            )
            plus_di = float(plus_di_arr[-1]) if len(plus_di_arr) > 0 and not np.isnan(plus_di_arr[-1]) else 20.0
            minus_di = float(minus_di_arr[-1]) if len(minus_di_arr) > 0 and not np.isnan(minus_di_arr[-1]) else 20.0
        except Exception:
            plus_di, minus_di = 20.0, 20.0
        
        # Calcular slope de EMA50 (primera derivada normalizada)
        ema50_series = df_1h['close'].ewm(span=50, adjust=False).mean()
        if len(ema50_series) >= 2:
            ema50_slope = (ema50_series.iloc[-1] - ema50_series.iloc[-2]) / ema50_series.iloc[-2]
        else:
            ema50_slope = 0.0
        
        # Actualizar MACRO state en RegimeFusion
        self._regime_fusion.update_macro(
            ema50=ema50_1h,
            ema200=ema200_1h,
            plus_di=plus_di,
            minus_di=minus_di,
            ema50_slope=ema50_slope
        )
        
        # Guardar estado macro para tags y logs
        macro_state = self._regime_fusion.regime_state
        self._current_trade_tags['regime_macro_score'] = macro_state.macro_score
        self._current_trade_tags['regime_macro_dir'] = macro_state.macro_dir
        self._current_trade_tags['regime_macro_strength'] = macro_state.macro_strength
        self._current_trade_tags['regime_macro'] = macro_state.regime.value
        
        # Log cambios significativos en macro
        if hasattr(self, '_last_logged_macro_regime') and self._last_logged_macro_regime != macro_state.regime:
            logger.info(
                f"🔄 MACRO REGIME CHANGE: {self._last_logged_macro_regime.value if self._last_logged_macro_regime else 'None'} → "
                f"{macro_state.regime.value} | score={macro_state.macro_score:.2f} | "
                f"+DI={plus_di:.1f} -DI={minus_di:.1f}"
            )
        self._last_logged_macro_regime = macro_state.regime
        
        # =========================================================================
        # 🧠 DECISION CENTER - INTELLIGENT TRADE GATING (Feb 06, 2026)
        # =========================================================================
        # Uses TrendIntelligence to make SMART decisions based on:
        # - MACRO: TrendPhase (BIRTH/ACCEL/MATURE/EXHAUST/DEATH)
        # - MACRO: Direction (BULLISH/BEARISH/NEUTRAL)
        # - MACRO: Confidence level
        #
        # This replaces the 20+ lagging filters with ONE intelligent decision:
        # "Is this the RIGHT phase and direction to enter this trade?"
        #
        # Key insight: TREND_BIRTH and TREND_ACCELERATION are the BEST phases.
        # TREND_EXHAUSTION and DISTRIBUTION should NOT have new entries.
        # =========================================================================
        
        # 🔥 FIX (Feb 06, 2026): Use ACTIVE direction, not HTF bias
        # The EdgeManager already decided "I want SHORT" based on analysis
        # Use that direction to check if TrendIntelligence allows it
        ti_check_direction = bias  # Default to HTF bias
        if self._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
            if self._edge_manager.current_bos:
                ti_check_direction = self._edge_manager.current_bos.direction
        elif self._active_direction:
            ti_check_direction = self._active_direction
        
        entry_recommendation = self._trend_intelligence.get_entry_recommendation(ti_check_direction)
        self._current_trade_tags['ti_allowed'] = entry_recommendation['allowed']
        self._current_trade_tags['ti_phase'] = entry_recommendation['phase']
        self._current_trade_tags['ti_direction_bias'] = entry_recommendation['direction_bias']
        self._current_trade_tags['ti_entry_mode'] = entry_recommendation['entry_mode']
        self._current_trade_tags['ti_size_mult'] = entry_recommendation['size_multiplier']
        self._current_trade_tags['ti_score_adj'] = entry_recommendation['score_adjustment']
        self._current_trade_tags['ti_confidence'] = entry_recommendation['confidence']
        
        # INTELLIGENT GATING based on TrendIntelligence
        if not entry_recommendation['allowed']:
            # Check if we have a displacement/edge that overrides
            edge_state = self._edge_manager.state
            edge_bos = self._edge_manager.current_bos
            has_edge_override = (
                edge_state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE] and
                edge_bos is not None
            )
            has_displacement_override = self._displacement_bypass
            
            # 🔥 DEBUG: Log what we're checking
            logger.debug(
                f"🔍 TI CHECK: allowed={entry_recommendation['allowed']} | "
                f"edge_state={edge_state.value if edge_state else 'None'} | "
                f"edge_bos={edge_bos is not None} | "
                f"has_edge_override={has_edge_override} | "
                f"displacement={has_displacement_override}"
            )
            
            # 🔥 FIX (Feb 06, 2026): ANY EDGE should override TI block, not just BREAKOUT/DISPLACEMENT
            # If EdgeManager has a signal, trust it - that's the whole point of having EdgeManager
            if has_edge_override or has_displacement_override:
                override_type = "DISPLACEMENT" if has_displacement_override else (
                    edge_bos.edge_type.value if edge_bos else "EDGE"
                )
                logger.info(
                    f"🌊 TI OVERRIDE: {entry_recommendation['reason']} but {override_type} active → allowing"
                )
                self._current_trade_tags['ti_overridden'] = True
            else:
                # Only block if confidence is high enough AND no edge
                if entry_recommendation['confidence'] >= 0.8:  # Raised from 0.6 to 0.8
                    logger.warning(
                        f"🧠 DECISION CENTER BLOCK: {entry_recommendation['reason']} | "
                        f"Confidence: {entry_recommendation['confidence']:.0%}"
                    )
                    self._current_trade_tags['skip_reason'] = f"ti_block_{entry_recommendation['phase']}"
                    return
                else:
                    logger.info(
                        f"📊 TI LOW CONFIDENCE ({entry_recommendation['confidence']:.0%}): "
                        f"{entry_recommendation['reason']} - proceeding with caution"
                    )
                    self._current_trade_tags['ti_low_confidence_proceed'] = True
        
        # Apply TI size multiplier for sizing decisions later
        self._ti_size_multiplier = entry_recommendation['size_multiplier']
        self._ti_score_adjustment = entry_recommendation['score_adjustment']
        
        # 🧠 ADDITIONAL INTELLIGENCE: Volume and Structure Analysis
        ti_state = self._trend_intelligence.state
        self._current_trade_tags['ti_volume_trend'] = ti_state.volume_trend
        self._current_trade_tags['ti_volume_confirms'] = ti_state.volume_on_moves
        self._current_trade_tags['ti_hh_count'] = ti_state.higher_highs_count
        self._current_trade_tags['ti_ll_count'] = ti_state.lower_lows_count
        self._current_trade_tags['ti_adx_slope'] = ti_state.adx_slope
        self._current_trade_tags['ti_adx_accel'] = ti_state.adx_acceleration
        
        # Volume divergence = reduce conviction
        if ti_state.volume_on_moves == "diverges":
            self._ti_size_multiplier *= 0.7
            # 🔧 FIX #129: Throttle log to once per 2 minutes
            market_time = self.market_data.get_market_time()
            if self._last_vol_divergence_log is None or \
               (market_time - self._last_vol_divergence_log).total_seconds() >= 120:
                logger.info(f"📉 VOLUME DIVERGENCE: Reducing size by 30%")
                self._last_vol_divergence_log = market_time
            self._current_trade_tags['ti_volume_penalty'] = True
        
        # Structure alignment bonus
        if bias == 'LONG' and ti_state.higher_highs_count >= 2:
            self._ti_score_adjustment -= 0.5  # Lower threshold (easier entry)
            self._current_trade_tags['ti_structure_aligned'] = True
        elif bias == 'SHORT' and ti_state.lower_lows_count >= 2:
            self._ti_score_adjustment -= 0.5
            self._current_trade_tags['ti_structure_aligned'] = True
        
        # ADX acceleration: if momentum is building, boost size
        if ti_state.adx_acceleration > 1.0:
            self._ti_size_multiplier = min(1.3, self._ti_size_multiplier * 1.1)
            logger.debug(f"📈 ADX ACCELERATION +: Size boost (accel={ti_state.adx_acceleration:.2f})")
        elif ti_state.adx_acceleration < -1.0:
            self._ti_size_multiplier *= 0.85
            logger.debug(f"📉 ADX DECELERATION: Size reduction (accel={ti_state.adx_acceleration:.2f})")
        
        # Log significant recommendations (rate-limited to avoid spam)
        if entry_recommendation['entry_mode'] == 'AGGRESSIVE':
            # 🔥 FIX #151 (Feb 10, 2026): Rate-limit AGGRESSIVE log to 60s
            now = market_time
            last_agg_log = getattr(self, '_last_ti_aggressive_log', None)
            if last_agg_log is None or (now - last_agg_log).total_seconds() >= 60:
                logger.info(
                    f"🚀 TI AGGRESSIVE MODE: {entry_recommendation['phase']} | "
                    f"Direction: {entry_recommendation['direction_bias']} | "
                    f"Best entry opportunity!"
                )
                self._last_ti_aggressive_log = now
        elif entry_recommendation['entry_mode'] == 'WAIT':
            # Even if allowed, WAIT mode = lower conviction
            logger.debug(f"⏳ TI WAIT MODE: {entry_recommendation['phase']} - proceed with reduced size")
        
        # =========================================================================
        # 🔥 FIX #83 (Feb 07, 2026): 3-LAYER REGIME FUSION - TRADE DECISION
        # 
        # Reemplaza la lógica fragmentada de counter-preference con:
        # - MICRO: direction + confidence + displacement/structure
        # - FUSION: alignment-based sizing y RR, ÚNICO bloqueo para fade estúpido
        # =========================================================================
        
        # Determinar si hay edge explosivo (displacement/structure)
        has_displacement = self._displacement_bypass or self._strong_displacement
        has_structure = (
            self._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE] and
            self._edge_manager.current_bos is not None
        )
        
        # Calcular confianza del micro signal
        micro_confidence = 0.5  # Base
        if has_displacement:
            micro_confidence = 0.9  # Alta confianza con displacement
        elif has_structure:
            micro_confidence = 0.7  # Buena confianza con estructura
        
        # Actualizar MICRO state
        self._regime_fusion.update_micro(
            direction=bias,  # "LONG" o "SHORT"
            confidence=micro_confidence,
            has_displacement=has_displacement,
            has_structure=has_structure,
        )
        
        # FUSIÓN: Obtener decisión final
        regime_decision = self._regime_fusion.fuse(micro_direction=bias)
        self._last_regime_decision = regime_decision
        
        # Guardar en tags
        self._current_trade_tags['regime_alignment'] = regime_decision.alignment
        self._current_trade_tags['regime_size_mult'] = regime_decision.size_mult
        self._current_trade_tags['regime_rr_min'] = regime_decision.rr_min
        self._current_trade_tags['regime_is_fade'] = regime_decision.is_fade
        self._current_trade_tags['regime_blocked'] = regime_decision.is_blocked
        
        # =====================================================================
        # 🔥 ÚNICO BLOQUEO: Fade estúpido (macro fuerte + contra + sin edge)
        # =====================================================================
        if regime_decision.is_blocked:
            logger.warning(
                f"☠️ REGIME FUSION BLOCK: {regime_decision.reason} | "
                f"macro={self._regime_fusion.regime_state.regime.value}, "
                f"score={self._regime_fusion.regime_state.macro_score:.2f}, "
                f"micro={bias}"
            )
            self._current_trade_tags['skip_reason'] = 'regime_fade_block'
            return
        
        # Log decisión de fusión (rate limited)
        if regime_decision.is_fade:
            should_log_fusion = (
                self._last_countertrend_log is None or 
                (market_time - self._last_countertrend_log) > self._log_rate_limit_interval
            )
            if should_log_fusion:
                self._last_countertrend_log = market_time
                logger.info(
                    f"🔀 REGIME FADE ALLOWED: {bias} vs macro={self._regime_fusion.regime_state.regime.value} | "
                    f"size={regime_decision.size_mult:.0%}, rr_min={regime_decision.rr_min:.1f}R | "
                    f"override={regime_decision.override_type or 'none'}"
                )
        
        # Usar los valores del RegimeFusion en lugar de counter_pref
        self._current_trade_tags['is_counter_preference'] = regime_decision.is_fade
        self._current_trade_tags['counter_pref_size_mult'] = regime_decision.size_mult
        self._current_trade_tags['counter_pref_rr_add'] = regime_decision.rr_min - 1.0  # Compatibilidad
        
        # 2. Get old countertrend rules for compatibility (kept for logs only)
        ct_rules = BiasStrengthConfig.get_countertrend_rules(self._current_bias_strength, bias)
        is_countertrend = ct_rules['is_countertrend']
        
        self._current_trade_tags['is_countertrend'] = is_countertrend
        self._current_trade_tags['ct_size_mult'] = regime_decision.size_mult  # Usar fusion
        self._current_trade_tags['ct_max_rr'] = ct_rules['max_rr']
        
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
        
        # 4. Apply anti-fade rule (reduce size in expansion, don't block)
        # 🔥 FIX #83: Use regime_decision.is_fade instead of is_counter_preference
        if self._anti_fade_until and market_time < self._anti_fade_until:
            if regime_decision.is_fade:
                remaining = (self._anti_fade_until - market_time).total_seconds() / 60
                # Rate limit anti-fade log
                should_log_af = (
                    self._last_antifade_log is None or
                    (market_time - self._last_antifade_log) > self._log_rate_limit_interval
                )
                if should_log_af:
                    self._last_antifade_log = market_time
                    logger.info(f"⚠️ ANTI-FADE: Fade during expansion ({remaining:.0f}min remaining) | Size reduced 50%")
                # Reduce size instead of blocking
                self._current_trade_tags['regime_size_mult'] *= 0.5
        
        # =====================================================================
        # 5. Short Obstinacy Check - 🔥 FIX #135: ELIMINATED (Feb 10, 2026)
        # =====================================================================
        # REASON FOR REMOVAL: SHORT OBSTINACY checked lagging signals (VWAP, 
        # EMA20>EMA50) that react AFTER price moved. This blocked good trades.
        # The STRUCTURE GATE (EMA50_1h alignment) is the correct filter.
        # This entire feature was disabled via SIMPLE_MODE=True since Feb 06.
        # Now removed completely to reduce code complexity.
        # =====================================================================
        self._current_trade_tags['short_obstinacy_disabled'] = True
        
        # ===== BLOQUE 2.5: MICRO TREND FILTER (5m) =====
        candles_1m_list = self.market_data.get_candles('1m')
        candles_5m_list = self.market_data.get_candles('5m')
        
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
                    # 🔥 FIX #135: MICRO TREND VETO disabled - only context tagging, no blocking
                    self._current_trade_tags['micro_trend_veto'] = True
                    self._current_trade_tags['micro_veto_reason'] = micro_reason
                    # No block - just log for context
                    logger.debug(f"📊 [CONTEXT] Micro Trend opposes {bias} - no block (FIX #135)")
            else:
                self._last_micro_state = f'allowed_{bias}'
                if self._micro_trend_state and self._micro_trend_state.score_adjustment != 0:
                    logger.debug(f"📊 MICRO TREND: {self._micro_trend_state.micro_trend} (strength={self._micro_trend_state.strength:.0%}, score_adj={self._micro_trend_state.score_adjustment:+.1f})")
        else:
            # Not enough data - allow but log
            self._micro_trend_state = None
            logger.debug("⚠️ MICRO TREND: Insufficient 5m data - skipping filter")
        
        # =========================================================================
        # =========================================================================
        # 🔥 FIX #135: BIAS INVERSION - ELIMINATED (Feb 10, 2026)
        # =========================================================================
        # REASON FOR REMOVAL: BIAS INVERSION caused countertrend trades when 
        # micro-trend was "strong" but was just a pullback within a larger trend.
        # The HTF bias (EMA50_1h) is the correct directional filter.
        # This feature was disabled via SIMPLE_MODE=True since Feb 06.
        # Now removed completely to reduce code complexity.
        # 
        # Context tagging preserved for analysis:
        # =========================================================================
        MICRO_STRENGTH_FOR_INVERSION = 0.65
        if self._micro_trend_state is not None and hasattr(self._micro_trend_state, 'strength'):
            if self._micro_trend_state.strength >= MICRO_STRENGTH_FOR_INVERSION:
                micro_trend = self._micro_trend_state.micro_trend
                if (bias == 'LONG' and micro_trend == 'BEARISH') or (bias == 'SHORT' and micro_trend == 'BULLISH'):
                    self._current_trade_tags['bias_inversion_would_trigger'] = True
                    self._current_trade_tags['micro_trend_opposing'] = micro_trend
                    self._current_trade_tags['micro_trend_strength'] = self._micro_trend_state.strength
                    logger.debug(
                        f"📊 [CONTEXT] BIAS INVERSION would trigger: HTF={bias}, micro={micro_trend} "
                        f"(strength={self._micro_trend_state.strength:.0%}) - disabled (FIX #135)"
                    )
        
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
                            # 🔥 FIX #135: EMA Distance VETO eliminated - context tagging only
                            # REASON: This veto was too aggressive and blocked entries at mean-reversion opportunities
                            self._current_trade_tags['ema_distance_veto_would_trigger'] = True
                            self._current_trade_tags['ema20_distance_pct'] = distance_to_ema20 * 100
                            self._current_trade_tags['ema50_distance_pct'] = distance_to_ema50 * 100
                            logger.debug(
                                f"📊 [CONTEXT] EMA Distance: LONG {abs(distance_to_ema20)*100:.1f}% below EMA20 (veto disabled FIX #135)"
                            )
                    
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
                            # 🔥 FIX #135: EMA Distance VETO eliminated - context tagging only
                            # REASON: This veto was too aggressive and blocked entries at mean-reversion opportunities
                            self._current_trade_tags['ema_distance_veto_would_trigger'] = True
                            self._current_trade_tags['ema20_distance_pct'] = distance_to_ema20 * 100
                            self._current_trade_tags['ema50_distance_pct'] = distance_to_ema50 * 100
                            logger.debug(
                                f"📊 [CONTEXT] EMA Distance: SHORT {distance_to_ema20*100:.1f}% above EMA20 (veto disabled FIX #135)"
                            )
        
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
        
        # 🔥 FIX #46 (Feb 05): Check if ANTICIPATION bypass should be active
        # ANTICIPATION has its own confirmation - bypasses macro slope (reduces size, not blocks)
        # 🔥 FIX #95 (Feb 09): Aceptar todos los tipos de anticipation
        valid_anticipation_signals = ['COMPRESSION_BREAKOUT', 'TREND_PULLBACK', 'FAILED_TEST', 'MOMENTUM_EXHAUSTION']
        anticipation_bypass = (
            self._current_trade_tags.get('anticipation_signal') in valid_anticipation_signals and
            self._current_trade_tags.get('anticipation_confidence', 0) >= 0.55
        )
        
        # 🔥 FIX #67 (Feb 06): Check if BIAS FLIP bypass should be active
        # BIAS FLIP = market structure changed, old sweeps are stale
        bias_flip_bypass = (
            self._current_trade_tags.get('bias_invalidated', False) or
            self._current_trade_tags.get('bias_inverted', False)
        )
        
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
            displacement_bypass=self._displacement_bypass,  # 🔥 FIX (Jan 29): Pass displacement state
            anticipation_bypass=anticipation_bypass,  # 🔥 FIX #46 (Feb 05): ANTICIPATION bypasses macro slope
            bias_flip_bypass=bias_flip_bypass  # 🔥 FIX #67 (Feb 06): BIAS FLIP bypasses sweep
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
        
        # 🔥 FIX #154 (Feb 10, 2026): LTF confirmations < 2 = HARD BLOCK
        # BEFORE: ltf_passed=True with 1/3 confirmations was allowed
        # NOW: Require at least 2/3 confirmations
        # Example: Trade 14:17:43 entered with LTF(1/3), MFE=0.00R, lost -0.44R
        # 
        # 🔥 FIX #162 (Feb 10): BYPASS en EXPANSION/CONSENSUS fuerte
        # Problema: LTF WEAK BLOCK bloqueaba trades válidos en tendencia
        # Solución: En EXPANSION o con edge fuerte, aceptar 1/3 confirmaciones
        ltf_confs = ltf_info.get('confirmations', 0) if ltf_info else 0
        min_ltf_required = 2  # Default
        
        # Reducir requisito en condiciones favorables
        if self._trend_expansion_active or self._expansion_mode:
            min_ltf_required = 2  # EXPANSION: 1/3 is enough
        elif self._edge_manager.state == EdgeState.EDGE_ACTIVE:
            bos = self._edge_manager.current_bos
            if bos and bos.trend_path and 'DISPLACEMENT' in str(bos.trend_path):
                min_ltf_required = 2  # DISPLACEMENT edge: 1/3 is enough
        
        if ltf_confs < min_ltf_required:
            if self._last_ltf_state != 'weak_ltf':
                logger.warning(f"⛔ LTF WEAK BLOCK: {ltf_confs}/3 confirmations < {min_ltf_required} minimum | Entry blocked")
                self._last_ltf_state = 'weak_ltf'
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
                    # 🔥 DISABLED (Feb 04, 2026): Delta contradict penalties removed
                    # Delta is lagging - was blocking valid entries and allowing bad ones
                    self._current_trade_tags['delta_contradicts'] = True
                    logger.debug(f"[INFO] Delta contradicts LONG (not penalized): {delta_ratio:.2f}x vol")
                elif bias == 'SHORT' and delta_ratio > 0.5:
                    # 🔥 DISABLED (Feb 04, 2026): Delta contradict penalties removed
                    self._current_trade_tags['delta_contradicts'] = True
                    logger.debug(f"[INFO] Delta contradicts SHORT (not penalized): +{delta_ratio:.2f}x vol")
                
                # 🔥 FIX #99 REMOVED (Feb 09, 2026): Delta NO invalida edge
                # El usuario confirmó que el sistema hubiera ganado con LONG
                # aunque el delta era negativo. El delta es indicativo, no determinante.
                
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
        #
        # NOTA (Feb 2, 2026): El orchestrator ahora maneja la prioridad del edge.
        # Este bloque solo sincroniza variables locales para logging.
        # =====================================================================
        # 🔥 FIX (Jan 30): First check if edge_type already stored (e.g., from DISPLACEMENT detection)
        # Only update from edge_manager if not already set
        edge_direction_final = self._current_trade_tags.get('edge_direction')
        edge_type_final = self._current_trade_tags.get('edge_type')
        
        # If not already set, try to get from edge_manager
        if not edge_type_final:
            if self._edge_manager.state in [EdgeState.EDGE_ARMED, EdgeState.EDGE_ACTIVE]:
                if self._edge_manager.current_bos:
                    edge_direction_final = self._edge_manager.current_bos.direction
                    edge_type_final = self._edge_manager.current_bos.edge_type
                    if edge_type_final:
                        # Store as string for consistency
                        self._current_trade_tags['edge_type'] = edge_type_final.value if hasattr(edge_type_final, 'value') else edge_type_final
                        self._current_trade_tags['edge_direction'] = edge_direction_final
                        logger.debug(f"📊 Edge stored from manager: type={self._current_trade_tags['edge_type']}, dir={edge_direction_final}")
        else:
            logger.debug(f"📊 Edge already in tags: type={edge_type_final}, dir={edge_direction_final}")
        
        # Si hay BREAKOUT edge, sincronizar bias local para logging
        # NOTE: _active_direction ya fue seteado por el orchestrator con EDGE_BREAKOUT priority 95
        is_breakout = (
            edge_type_final == EdgeType.BREAKOUT or 
            edge_type_final == 'BREAKOUT' or
            (hasattr(edge_type_final, 'value') and edge_type_final.value == 'BREAKOUT')
        )
        if is_breakout and edge_direction_final:
            if bias != edge_direction_final:
                # Solo sincronizar bias local, NO modificar _active_direction
                # (ya fue seteado correctamente por el orchestrator)
                # 🔥 FIX #122: Throttle 60 → 180 ticks
                if self._idle_eval_count % 180 == 1:
                    logger.debug(
                        f"📊 Syncing local bias with edge: {bias} → {edge_direction_final}"
                    )
                bias = edge_direction_final
                self.current_bias = bias
                # NOTE: _active_direction override REMOVED - orchestrator handles this
        
        # ===== ALL FILTERS PASSED - PRE-CHECK EDGE CONTEXT =====
        # Note: LTF is "score-based" not "gate-based" so we show actual confirmations
        ltf_confs = self._current_trade_tags.get('ltf_confirmations', '?')
        
        # 🔥 FIX #151 (Feb 10, 2026): Rate-limit SETUP COMPLETE log to 60s
        # This log was spamming when guards block after filter pass
        now = market_time
        last_setup_log = getattr(self, '_last_setup_complete_log', None)
        should_log_setup = last_setup_log is None or (now - last_setup_log).total_seconds() >= 60
        
        # Quick edge context check BEFORE showing "SETUP COMPLETE"
        # This prevents confusing logs where setup appears complete but is then blocked
        if self._edge_context and not self._edge_context.is_valid():
            if should_log_setup:
                logger.info(f"🎯 FILTERS PASSED - {bias} | HTF✓ MTF✓ LTF({ltf_confs}/3) | ⚠️ Edge context pending validation")
                self._last_setup_complete_log = now
        else:
            # 🔥 FIX: Log edge info if present
            edge_info = ""
            if edge_type_final and edge_direction_final:
                # edge_type_final may be string or Enum
                edge_type_str = edge_type_final.value if hasattr(edge_type_final, 'value') else edge_type_final
                edge_info = f" | Edge: {edge_type_str} {edge_direction_final}"
            if should_log_setup:
                logger.info(f"🎯 SETUP COMPLETE - {bias} | HTF✓ MTF✓ LTF({ltf_confs}/3){edge_info}")
                self._last_setup_complete_log = now
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
        # 🧠 Apply TrendIntelligence size multiplier (Feb 06, 2026)
        risk_pct = OperatingMode.get_risk_pct() / 100.0
        ti_mult = getattr(self, '_ti_size_multiplier', 1.0)
        risk_pct_adjusted = risk_pct * signal.size_factor * ti_mult
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
                
                # =========================================================================
                # 🔥 FIX #111 (Feb 09, 2026): EXECUTION SLA VIOLATION DETECTION
                #
                # PROBLEMA: El sistema ve trades (EDGE_ACTIVE + SETUP_COMPLETE) pero
                # no los toma por penalties acumulativos. Esto viola el SLA de ejecución.
                #
                # SOLUCIÓN: Si hay EDGE_ACTIVE y conf >= 70% pero no entramos,
                # loguear ERROR para análisis posterior. Esto ayuda a detectar
                # sobre-filtraje sistémico.
                # =========================================================================
                edge_state = getattr(self._edge_manager, 'state', None)
                from src.domain.trading.edge_state_manager import EdgeState
                if edge_state in [EdgeState.EDGE_ACTIVE]:
                    # Check confidence and other factors
                    current_conf = self._current_trade_tags.get('direction_confidence', 0)
                    transition_mode = getattr(self, '_transition_mode_active', False)
                    
                    if current_conf >= 70:
                        logger.error(
                            f"🚨 EXECUTION SLA VIOLATION: Edge={edge_state.value}, "
                            f"Conf={current_conf}%, bias={self.current_bias} | "
                            f"Reason: {result.reason} | TransitionMode={transition_mode}"
                        )
                    
        except Exception as e:
            # 🔥 FIX #86 (Feb 09, 2026): NEVER cooldown for CODE ERRORS
            # Code errors ≠ trading errors. Cooldown only for market-related issues.
            # Log critically and continue - don't punish for bugs.
            logger.critical(f"Entry orchestration error (NO COOLDOWN): {e}", exc_info=True)
            self._reset_trade_state("Entry orchestration exception")
            
            # Track consecutive errors for circuit breaker (FASE 3)
            if not hasattr(self, '_consecutive_code_errors'):
                self._consecutive_code_errors = 0
                self._first_error_time = None
            
            self._consecutive_code_errors += 1
            if self._first_error_time is None:
                self._first_error_time = market_time
            
            # Circuit breaker: 3 errors in 10 min = alert + brief pause
            if self._consecutive_code_errors >= 3:
                error_window = (market_time - self._first_error_time).total_seconds()
                if error_window < 600:  # 10 min
                    logger.critical(f"🚨 CIRCUIT BREAKER: {self._consecutive_code_errors} code errors in {error_window:.0f}s")
                    # Only 30s pause for investigation, NOT 5min punishment
                    self.state_machine.start_cooldown(1)  # 1 minute (int required)
                # Reset counter regardless
                self._consecutive_code_errors = 0
                self._first_error_time = None
            # NO self.state_machine.start_cooldown(5) - THIS WAS THE BUG
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
                            regime_mode=self._current_trade_regime,  # 🔥 FIX (Jan 22): Pass regime to trailing
                            edge_type=self._current_trade_tags.get('edge_type'),  # 🔥 FIX (Jan 29): Pass edge_type for TIME_INVALIDATION
                            entry_score=self._current_trade_tags.get('entry_score'),  # 🔥 FIX (Feb 01): Pass entry_score for stricter TIME_INVALIDATION
                            is_weekend=market_time.weekday() >= 5 if market_time else False  # 🔥 FIX (Feb 01): Weekend stricter exits
                        )
                        
                        # 🔥 NEW: Start exhaustion tracking for this trade
                        self.exhaustion_detector.start_tracking(market_time)
                        self._exhaustion_tightened = False
                        
                        # 🔥 FIX (Feb 01): Reset gap history for new trade
                        self._gap_history = []
                        
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
                logger.info(f"📊 FIX #78: POST_ONLY rejection count: {self._consecutive_post_only_rejects}/3")
                
                # =============================================================
                # 🔥 FIX #78 (Feb 06, 2026): POST-ONLY RETRY MECHANISM
                # Instead of immediately giving up:
                # 1. Retry 3 times with price adjustment (+/- tick to win spread)
                # 2. After 3 failures, fallback to MARKET order at 0.5× size
                # =============================================================
                MAX_RETRIES = 3
                PRICE_ADJUSTMENT_TICKS = 1.0  # $1 adjustment per retry
                FALLBACK_SIZE_MULTIPLIER = 0.5  # Market order at half size
                
                entry_data = getattr(self, '_last_entry_data', None)
                
                if entry_data:
                    bias = entry_data.get('bias', 'LONG')
                    original_price = entry_data.get('entry_price', 0)
                    original_size = entry_data.get('position_size', 0)
                    stop_loss = entry_data.get('stop_loss', 0)
                    take_profit = entry_data.get('take_profit', 0)
                    
                    if self._consecutive_post_only_rejects < MAX_RETRIES:
                        # ==========================================
                        # RETRY WITH PRICE ADJUSTMENT
                        # ==========================================
                        adjustment = PRICE_ADJUSTMENT_TICKS * self._consecutive_post_only_rejects
                        
                        if bias == 'LONG':
                            adjusted_price = original_price - adjustment
                        else:
                            adjusted_price = original_price + adjustment
                        
                        logger.info(f"🔄 FIX #78: RETRY {self._consecutive_post_only_rejects}/{MAX_RETRIES} - "
                                   f"{bias} @ {adjusted_price:.2f} (adjusted from {original_price:.2f})")
                        
                        try:
                            order_result = await self.execution.place_limit_order(
                                symbol=self.symbol,
                                side='buy' if bias == 'LONG' else 'sell',
                                amount=original_size,
                                price=adjusted_price,
                                stop_loss=stop_loss,
                                take_profit=take_profit
                            )
                            
                            if order_result and order_result.get('id'):
                                logger.info(f"✅ FIX #78: RETRY ORDER PLACED: {order_result['id']}")
                                
                                # Update pending order - stay in WAIT_FILL
                                self.pending_order_id = order_result['id']
                                self.entry_order_time = market_time  # Use market_time for timezone consistency
                                
                                # Notify reconciler
                                self.reconciler.notify_order_placed(order_result['id'])
                                
                                # Update entry data with new price
                                if self._last_entry_data is not None:
                                    self._last_entry_data['entry_price'] = adjusted_price
                                    self._last_entry_data['tags']['post_only_retry'] = self._consecutive_post_only_rejects
                                    self._last_entry_data['tags']['original_price'] = original_price
                                
                                return  # Stay in WAIT_FILL with new order
                                
                        except Exception as retry_err:
                            logger.error(f"❌ FIX #78: Retry order failed: {retry_err}")
                    
                    # ==========================================
                    # MAX RETRIES REACHED → MARKET ORDER FALLBACK
                    # ==========================================
                    if self._consecutive_post_only_rejects >= MAX_RETRIES:
                        reduced_size = original_size * FALLBACK_SIZE_MULTIPLIER
                        
                        logger.warning(f"🚨 FIX #78: MAX RETRIES ({MAX_RETRIES}) REACHED - "
                                      f"FALLBACK to MARKET @ {reduced_size:.6f} (0.5× original)")
                        
                        try:
                            order_result = await self.execution.place_aggressive_order(
                                symbol=self.symbol,
                                side='buy' if bias == 'LONG' else 'sell',
                                amount=reduced_size,
                                price=original_price,
                                stop_loss=stop_loss,
                                take_profit=take_profit
                            )
                            
                            if order_result and order_result.get('id'):
                                logger.info(f"✅ FIX #78: MARKET FALLBACK PLACED: {order_result['id']} @ {reduced_size:.6f}")
                                
                                # Reset counter - we got an order placed
                                self._consecutive_post_only_rejects = 0
                                
                                # Update state
                                self.pending_order_id = order_result['id']
                                self.entry_order_time = market_time  # Use market_time for timezone consistency
                                
                                # Notify reconciler
                                self.reconciler.notify_order_placed(order_result['id'])
                                
                                # Update entry data
                                if self._last_entry_data is not None:
                                    self._last_entry_data['position_size'] = reduced_size
                                    self._last_entry_data['order_type'] = 'MARKET'
                                    self._last_entry_data['tags']['post_only_fallback'] = True
                                    self._last_entry_data['tags']['fallback_size_reduction'] = FALLBACK_SIZE_MULTIPLIER
                                
                                return  # Stay in WAIT_FILL with market order
                                
                        except Exception as fallback_err:
                            logger.error(f"❌ FIX #78: Market fallback error: {fallback_err}")
                
                # ALL RETRIES FAILED OR NO ENTRY DATA - RESET TO IDLE
                logger.error(f"❌ FIX #78: All retry attempts exhausted - resetting to IDLE")
                self._consecutive_post_only_rejects = 0
                self._reset_trade_state("Order rejected after all retries")
                self.state_machine.transition_to(BotState.IDLE, "Order rejected - max retries")
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
                        regime_mode=self._current_trade_regime,  # 🔥 FIX (Jan 22): Pass regime to trailing
                        edge_type=self._current_trade_tags.get('edge_type'),  # 🔥 FIX (Jan 29): Pass edge_type for TIME_INVALIDATION
                        entry_score=self._current_trade_tags.get('entry_score'),  # 🔥 FIX (Feb 01): Pass entry_score for stricter TIME_INVALIDATION
                        is_weekend=market_time.weekday() >= 5 if market_time else False  # 🔥 FIX (Feb 01): Weekend stricter exits
                    )
                    
                    # 🔥 NEW: Start exhaustion tracking for this trade
                    self.exhaustion_detector.start_tracking(market_time)
                    self._exhaustion_tightened = False
                    
                    # 🔥 FIX (Feb 01): Reset gap history for new trade
                    self._gap_history = []
                    
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
                        
                        # 🔥 EDGE TYPE (Feb 05, 2026): Get edge_type from tags for logging
                        edge_type_for_log = trade_tags.get('edge_type', '') if isinstance(trade_tags, dict) else ''
                        
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
                            entry_daily_regime=self._daily_regime,
                            # 🔥 EDGE TYPE (Feb 05, 2026)
                            edge_type=edge_type_for_log
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
                        # 🔥 FIX #27: Initialize _last_sent_sl for trailing sync
                        self._last_sent_sl = self._last_entry_data['stop_loss']
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
            # ============================================================
            # 🔥 FIX #79 (Feb 07, 2026): CHECK EXCHANGE BEFORE IDLE
            # 
            # PROBLEM: Si hay posición en exchange pero no local, volver
            # a IDLE causaba entradas duplicadas.
            # 
            # SOLUTION: Verificar exchange primero. Si hay posición,
            # sincronizar en lugar de ir a IDLE.
            # ============================================================
            try:
                exchange_has_position = await self.execution.has_open_position(self.symbol)
                if exchange_has_position:
                    logger.warning("🔒 FIX #79: No local position but EXCHANGE HAS POSITION - syncing instead of IDLE")
                    
                    # Get exchange position details
                    exchange_pos = await self.execution.get_position(self.symbol)
                    if exchange_pos:
                        size = abs(float(exchange_pos.get('contracts', 0) or 0))
                        side = exchange_pos.get('side', '').lower()
                        entry_price = float(exchange_pos.get('entryPrice', 0) or 0)
                        
                        if size >= KRAKEN_BTC_MIN_SIZE:
                            # Create local position to match exchange
                            logger.info(f"🔄 FIX #79: Creating local position from exchange: {side} {size} @ {entry_price}")
                            
                            direction = 'LONG' if side == 'long' else 'SHORT'
                            # Use 1% of entry price as fallback ATR
                            fallback_atr = entry_price * 0.01
                            self.risk_engine.create_position(
                                symbol=self.symbol,
                                direction=direction,
                                entry_price=entry_price,
                                position_size=size,
                                stop_loss=entry_price * (0.99 if direction == 'LONG' else 1.01),  # Default SL 1%
                                atr_1m=fallback_atr,
                                market_time=market_time,
                                trade_type=TradeType.LONG_MAIN if direction == 'LONG' else TradeType.SHORT_STRUCTURAL,
                                regime_mode=self._current_trade_regime,
                            )
                            
                            # Start cooldown to prevent immediate re-entry
                            self.state_machine.start_cooldown(3)
                            logger.info(f"✅ FIX #79: Position synced - staying in MANAGE with 3-candle cooldown")
                            return  # Stay in MANAGE
                            
            except Exception as sync_err:
                logger.error(f"❌ FIX #79: Exchange sync failed: {sync_err}")
            
            # 🔒 LIMPIEZA COMPLETA si no hay posición local NI en exchange
            logger.warning("MANAGE state but no local position AND no exchange position - transitioning to IDLE")
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
            # 🔧 FIX #23 (Feb 02, 2026): Use ACTUAL ATR from market data, not position's entry ATR
            # The bug was: current_atr = position.atr_1m compares ATR with itself (ratio=1.0)
            # Now we calculate fresh ATR from current candles
            candles_1m_for_atr = self.market_data.get_candles('1m')
            if candles_1m_for_atr and len(candles_1m_for_atr) >= 20:
                df_temp = pd.DataFrame(candles_1m_for_atr)
                atr_values = calculate_atr(df_temp['high'].to_numpy(), df_temp['low'].to_numpy(), df_temp['close'].to_numpy())
                current_atr = float(atr_values[-1]) if len(atr_values) > 0 else position.atr_1m
            else:
                current_atr = position.atr_1m  # Fallback if insufficient data
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
        # 🔥 FIX (Feb 01, 2026): INTELLIGENT PRICE GAP DETECTION
        #    - Problem: Single-tick gap (0.5%) triggered false EMERGENCY exits
        #    - Solution: Normalize gap by ATR + require 2 consecutive large gaps
        # =====================================================================
        price_gap_pct = None
        
        # 🔧 FIX (Jan 25, 2026): Use REAL feed staleness from market_data
        # Previous implementation calculated time between tick processing (buggy)
        last_tick_age_sec = self.market_data.get_feed_age_sec()
        
        # 🔥 FIX (Feb 01, 2026): INTELLIGENT PRICE GAP DETECTION
        # Single tick gap is noisy - require confirmation
        # Gap normalized by ATR (1-min ATR ~ 0.05% for BTC)
        if self._prev_tick_price > 0 and current_price > 0:
            raw_gap_pct = ((current_price - self._prev_tick_price) / self._prev_tick_price) * 100
            
            # Initialize gap history if not exists
            if not hasattr(self, '_gap_history'):
                self._gap_history = []
            
            # Store gap in history (keep last 3 ticks)
            self._gap_history.append(raw_gap_pct)
            if len(self._gap_history) > 3:
                self._gap_history.pop(0)
            
            # INTELLIGENT GAP LOGIC:
            # - Single gap: Only trigger if MASSIVE (>1.5%)
            # - Multi-tick confirmation: If 2 of last 3 gaps are large (>0.8%), trigger
            # This prevents false positives from isolated tick spikes
            SINGLE_GAP_THRESHOLD = 1.5  # Single tick must be huge
            CONFIRMED_GAP_THRESHOLD = 0.8  # For multi-tick confirmation
            
            large_gaps_count = sum(1 for g in self._gap_history if abs(g) > CONFIRMED_GAP_THRESHOLD)
            
            if abs(raw_gap_pct) > SINGLE_GAP_THRESHOLD:
                # Massive single gap - immediate trigger
                price_gap_pct = raw_gap_pct
            elif len(self._gap_history) >= 2 and large_gaps_count >= 2:
                # Multi-tick confirmation: sustained abnormal movement
                price_gap_pct = raw_gap_pct
            else:
                # Normal volatility - don't trigger
                price_gap_pct = None
        
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
        
        # =====================================================================
        # 🔥 FIX #27 (Feb 02, 2026): SYNC TRAILING STOP WITH EXCHANGE
        # PROBLEMA: trailing_stop se calculaba localmente pero NUNCA se enviaba
        # al exchange. Si el bot se desconecta, el SL quedaba en valor inicial.
        #
        # SOLUCIÓN: Después de cada manage_position, sincronizar el trailing
        # con el exchange si cambió significativamente.
        # =====================================================================
        if position and not self._is_backtest:
            current_trailing = position.trailing_stop
            # Solo sincronizar si el trailing cambió significativamente (>0.3R o >0.5%)
            initial_risk = abs(position.entry_price - position.initial_stop_loss)
            min_move_r = initial_risk * 0.3  # 0.3R mínimo
            min_move_pct = position.entry_price * 0.005  # 0.5% mínimo
            min_move = max(min_move_r, min_move_pct)
            
            trailing_improved = False
            if position.direction == 'LONG':
                trailing_improved = current_trailing > self._last_sent_sl + min_move
            else:
                trailing_improved = current_trailing < self._last_sent_sl - min_move
            
            if trailing_improved and current_trailing != self._last_sent_sl:
                try:
                    success, new_sent = await self.execution.update_stop_loss_on_exchange(
                        symbol=self.symbol,
                        new_sl_price=current_trailing,
                        last_sent_sl=self._last_sent_sl,
                        atr=atr_1m,
                        spread=self._last_spread_bps or 1.0,
                        initial_risk=initial_risk,
                        direction=position.direction
                    )
                    if success:
                        self._last_sent_sl = new_sent
                        logger.info(f"🔄 TRAILING SYNCED TO EXCHANGE: {new_sent:.2f}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to sync trailing to exchange: {e}")
        
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
        """Get indicators with caching - delegated to IndicatorCache."""
        if not hasattr(self, '_indicator_cache_handler'):
            self._indicator_cache_handler = IndicatorCache()
        return self._indicator_cache_handler.get_cached_indicators(candles_1m, candles_5m, candles_1h)

    def _calculate_atr_5m(self, df_5m: pd.DataFrame) -> float:
        """Calculate ATR from 5m timeframe - delegates to indicator_helpers"""
        return _calc_atr_5m_pure(df_5m)
    
    def _price_confirms_direction(self, direction: str, lookback_candles: int = 5) -> tuple[bool, float, str]:
        """
        🔥 FIX (Feb 04, 2026): Validate that PRICE MOVEMENT confirms the trade direction.
        🔥 FIX #53 (Feb 05, 2026): Extended lookback to 5 min, added high/low range check
        
        PROBLEMA: Sistema usaba CONSENSUS de indicadores lagging (structural == active)
        para entrar trades. Pero si el precio estaba moviéndose en dirección OPUESTA,
        entraba de todas formas. Ejemplo: SHORT cuando precio subió 0.5% en 3 min.
        
        SOLUCIÓN: Antes de cualquier entrada, verificar que el precio reciente
        se mueve EN LA DIRECCIÓN del trade, o al menos NO en contra.
        
        🔥 FIX #53: Also check if price made significant HIGH/LOW in lookback period
        - For SHORT: Block if recent high is significantly above current (price just rallied)
        - For LONG: Block if recent low is significantly below current (price just dumped)
        
        Args:
            direction: 'LONG' o 'SHORT'
            lookback_candles: Cuántas velas 1m mirar hacia atrás (default 5 = 5 min)
            
        Returns:
            (confirms: bool, price_change_pct: float, reason: str)
            - confirms: True si precio confirma o es neutral
            - price_change_pct: Cambio de precio en %
            - reason: Descripción del resultado
        """
        try:
            candles_1m = self.market_data.get_candles('1m')
            if not candles_1m or len(candles_1m) < lookback_candles + 1:
                return True, 0.0, "Not enough data - allowing entry"
            
            recent = candles_1m[-(lookback_candles + 1):]
            price_start = recent[0]['close']
            price_end = recent[-1]['close']
            price_change_pct = (price_end - price_start) / price_start * 100
            
            # 🔥 FIX #53: Also get the range extremes
            recent_high = max(c.get('high', c['close']) for c in recent)
            recent_low = min(c.get('low', c['close']) for c in recent)
            current_price = price_end
            
            # Distance from extremes
            dist_from_high_pct = (recent_high - current_price) / current_price * 100
            dist_from_low_pct = (current_price - recent_low) / current_price * 100
            
            # 🔥 FIX #79 (Feb 07): Raised threshold from 0.20% to 0.50%
            # Problem: 0.20% is too sensitive - normal volatility blocks good entries
            # Evidence: System blocked SHORT while price dropped $600 because of 0.2% bounces
            CONTRADICT_THRESHOLD = 0.50  # Was 0.20, now 0.50 (less sensitive)
            # 🔥 FIX #53: If price made a significant extreme and retraced, be cautious
            EXTREME_THRESHOLD = 0.60  # Was 0.35, now 0.60 (less sensitive)
            
            if direction == 'SHORT':
                # 🔥 FIX #53: Block if price made significant recent HIGH (just rallied)
                if dist_from_high_pct < 0.10 and price_change_pct > 0:
                    # Price is at/near recent high and climbing → don't short the top of a rally
                    return False, price_change_pct, f"SHORT blocked: at recent HIGH (dist={dist_from_high_pct:.2f}%, change={price_change_pct:.2f}%)"
                
                if price_change_pct > CONTRADICT_THRESHOLD:
                    # Price is UP but we want SHORT → CONTRADICTION
                    return False, price_change_pct, f"SHORT contradicted: price UP {price_change_pct:.2f}% (${price_start:.0f}→${price_end:.0f})"
                elif price_change_pct < -CONTRADICT_THRESHOLD:
                    # Price is DOWN and we want SHORT → CONFIRMS
                    return True, price_change_pct, f"SHORT confirmed: price DOWN {abs(price_change_pct):.2f}%"
                else:
                    # Neutral - but check if we just had a rally that retraced
                    if dist_from_high_pct > EXTREME_THRESHOLD:
                        # Price recently made a high and pulled back significantly
                        # This could be good for SHORT (failed rally)
                        return True, price_change_pct, f"SHORT opportunity: retraced from high ({dist_from_high_pct:.2f}%)"
                    return True, price_change_pct, f"Neutral: price change {price_change_pct:.2f}%"
                    
            elif direction == 'LONG':
                # 🔥 FIX #53: Block if price made significant recent LOW (just dumped)
                if dist_from_low_pct < 0.10 and price_change_pct < 0:
                    # Price is at/near recent low and falling → don't long the bottom of a dump
                    return False, price_change_pct, f"LONG blocked: at recent LOW (dist={dist_from_low_pct:.2f}%, change={price_change_pct:.2f}%)"
                
                if price_change_pct < -CONTRADICT_THRESHOLD:
                    # Price is DOWN but we want LONG → CONTRADICTION
                    return False, price_change_pct, f"LONG contradicted: price DOWN {abs(price_change_pct):.2f}% (${price_start:.0f}→${price_end:.0f})"
                elif price_change_pct > CONTRADICT_THRESHOLD:
                    # Price is UP and we want LONG → CONFIRMS
                    return True, price_change_pct, f"LONG confirmed: price UP {price_change_pct:.2f}%"
                else:
                    # Neutral - but check if we just had a dump that bounced
                    if dist_from_low_pct > EXTREME_THRESHOLD:
                        # Price recently made a low and bounced significantly
                        # This could be good for LONG (failed breakdown)
                        return True, price_change_pct, f"LONG opportunity: bounced from low ({dist_from_low_pct:.2f}%)"
                    return True, price_change_pct, f"Neutral: price change {price_change_pct:.2f}%"
            
            return True, price_change_pct, "Unknown direction"
            
        except Exception as e:
            logger.debug(f"Price confirmation check error: {e}")
            return True, 0.0, f"Error checking: {e}"
    
    # =========================================================================
    # 🔥 UNIFIED TRADING INTELLIGENCE (Feb 04, 2026)
    # Single source of truth - replaces scattered duplicate checks
    # =========================================================================
    
    @property
    def has_momentum_override(self) -> bool:
        """
        🔥 UNIFIED: Should we bypass regime filters due to strong momentum?
        
        Replaces 6+ scattered checks of: self._displacement_bypass or self._expansion_mode
        
        Returns True when:
        - Displacement detected (large price move)
        - Expansion mode active (directional trend locked)
        - Strong displacement override
        """
        return self._displacement_bypass or self._expansion_mode or self._strong_displacement
    
    def _get_effective_direction(self) -> tuple[str | None, str, float]:
        """
        🔥 UNIFIED: Get the SINGLE effective trading direction.
        
        Replaces the chaos of: structural_bias, active_direction, forbidden_direction,
        expansion_direction, direction_lock, etc.
        
        Returns:
            (direction, source, confidence)
            - direction: 'LONG', 'SHORT', or None
            - source: Where the direction came from
            - confidence: 0.0 to 1.0
        """
        # FIX #40: CHECK FORBIDDEN DIRECTION FIRST (PRIORITY 0)
        # This prevents ANY direction matching forbidden from being returned
        # 
        # 🔥 FIX #53 (Feb 05): "BE WATER" - Location extreme check
        # Instead of: ARM edge → BLOCK → EXPIRE → RE-ARM (infinite loop)
        # Now: Don't want that direction at all if in extreme zone (flows naturally)
        # 
        # 🔥 FIX #54 (Feb 05): BREAKOUT OVERRIDE - Allow trades in extreme if breakout confirmed
        # If breakout score >= 3 AND breakout direction matches → override the extreme block
        def _check_forbidden(direction: str, source: str, confidence: float) -> tuple[str | None, str, float]:
            if self._forbidden_direction and direction == self._forbidden_direction:
                logger.debug(f"🚫 {direction} FORBIDDEN | Source would be {source} | Returning None")
                return None, 'FORBIDDEN', 0.0
            
            # 🔥 BE WATER: Check location extreme - don't even WANT incompatible direction
            # percentile <= 10% (low extreme) → don't want SHORT (already at bottom)
            # percentile >= 90% (high extreme) → don't want LONG (already at top)
            # UNLESS: Breakout confirmed in same direction (genuine trend continuation)
            if self._location_context:
                # Check for breakout override FIRST
                breakout_override = False
                min_breakout_score = 3  # Same as LocationFilterOverride
                
                if direction == 'SHORT' and self._location_context.in_extreme_low:
                    # At bottom - but check if breaking down (continuation)
                    if (self._location_context.is_breakout_down and 
                        self._location_context.breakout_down_score >= min_breakout_score):
                        breakout_override = True
                        logger.info(f"🌊 BE WATER: SHORT in extreme low ALLOWED - breakout down "
                                   f"(score={self._location_context.breakout_down_score})")
                    
                    if not breakout_override:
                        return None, 'LOCATION_EXTREME_LOW', 0.0
                
                if direction == 'LONG' and self._location_context.in_extreme_high:
                    # At top - but check if breaking up (continuation)
                    if (self._location_context.is_breakout_up and 
                        self._location_context.breakout_up_score >= min_breakout_score):
                        breakout_override = True
                        logger.info(f"🌊 BE WATER: LONG in extreme high ALLOWED - breakout up "
                                   f"(score={self._location_context.breakout_up_score})")
                    
                    if not breakout_override:
                        return None, 'LOCATION_EXTREME_HIGH', 0.0
            
            return direction, source, confidence
        
        # PRIORITY 1: Expansion mode (locked direction)
        if self._expansion_mode and self._expansion_direction:
            return _check_forbidden(self._expansion_direction, 'EXPANSION_LOCK', 1.0)
        
        # PRIORITY 2: Direction lock from displacement
        if self._direction_lock:
            return _check_forbidden(self._direction_lock, 'DIRECTION_LOCK', 0.9)
        
        # PRIORITY 3: Active edge direction (ONLY if edge is actually ACTIVE, not expired)
        if hasattr(self, '_edge_manager') and self._edge_manager:
            if self._edge_manager.is_edge_active() and self._edge_manager.current_bos:
                edge_dir = self._edge_manager.current_bos.direction
                if edge_dir:
                    return _check_forbidden(edge_dir, 'EDGE_ACTIVE', 0.8)
        
        # PRIORITY 4: Displacement direction
        if self._displacement_bypass and self._active_direction:
            return _check_forbidden(self._active_direction, 'DISPLACEMENT', 0.7)
        
        # PRIORITY 5: Consensus (structural == active)
        if (self._structural_bias and self._active_direction and 
            self._structural_bias == self._active_direction):
            return _check_forbidden(self._structural_bias, 'CONSENSUS', 0.6)
        
        # PRIORITY 6: Active direction alone
        if self._active_direction:
            return _check_forbidden(self._active_direction, 'MOMENTUM', 0.5)
        
        # PRIORITY 7: Structural bias as fallback
        if self._structural_bias:
            return _check_forbidden(self._structural_bias, 'HTF_BIAS', 0.3)
        
        return None, 'NONE', 0.0
    
    def _should_bypass_regime_filter(self, filter_name: str) -> tuple[bool, str]:
        """
        🔥 UNIFIED: Should we bypass a specific regime filter?
        
        Consolidates all the scattered bypass logic into ONE place.
        
        Args:
            filter_name: 'RANGE_EXTREME', 'RANGE_VOLUME', 'TRANSITION_CONSENSUS', etc.
        
        Returns:
            (should_bypass, reason)
        """
        # Momentum override always bypasses regime filters
        if self.has_momentum_override:
            bypass_type = 'EXPANSION' if self._expansion_mode else 'DISPLACEMENT'
            return True, f"{bypass_type} override active"
        
        # Compression breakout bypasses RANGE filters
        # 🔥 FIX #95 (Feb 09): Aceptar todos los tipos de anticipation
        if filter_name.startswith('RANGE'):
            valid_anticipation_signals = ['COMPRESSION_BREAKOUT', 'TREND_PULLBACK', 'FAILED_TEST', 'MOMENTUM_EXHAUSTION']
            has_breakout = (
                self._current_trade_tags.get('anticipation_signal') in valid_anticipation_signals and
                self._current_trade_tags.get('anticipation_confidence', 0) >= 0.55
            )
            if has_breakout:
                return True, 'ANTICIPATION detected'
        
        return False, ''
    
    # 🔥 REMOVED Feb 04: _calculate_atr_generic - unused method
    
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
    
    def _check_loss_cooldown(self, market_time: datetime) -> tuple[bool, str]:
        """
        LOSS STREAK COOLDOWN + AUTO KILL-SWITCH
        
        � REFACTORED (Feb 04, 2026): Delegates to CooldownManager
        Single source of truth for all cooldown logic.
        
        Returns: (can_enter, reason)
        """
        # Sync current state to CooldownManager
        self.cooldown_manager.sync_from_app(
            cooldown_until=self._cooldown_until,
            rejection_cooldown_until=self._rejection_cooldown_until,
            short_cooldown_until=getattr(self, '_short_cooldown_until', None),
            consecutive_losses=self._consecutive_losses,
            kill_switch_active=self._kill_switch_active,
            tick_count=self._idle_eval_count
        )
        
        # Check if trading is allowed
        result = self.cooldown_manager.check_can_trade(market_time)
        
        if not result.can_trade:
            # Map cooldown type to appropriate message
            from src.application.live.cooldown_manager import CooldownType
            
            if result.blocking_cooldown == CooldownType.KILL_SWITCH:
                return False, "🔴 KILL SWITCH ACTIVE: Daily loss limit reached - no trading until tomorrow"
            elif result.blocking_cooldown == CooldownType.DEAD_HOURS:
                return False, f"🌙 DEAD HOURS BLOCKED: Waiting for next session"
            elif result.blocking_cooldown == CooldownType.LOSS_STREAK:
                remaining_min = result.remaining_seconds / 60
                return False, f"⏸️ LOSS COOLDOWN: {remaining_min:.0f} min remaining ({self._consecutive_losses} consecutive losses)"
            else:
                return False, result.reason
        
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
            
            # 🔥 FIX (Feb 05, 2026): Record direction outcome for circuit breaker
            try:
                from src.domain.trading.direction_orchestrator import get_direction_orchestrator
                orchestrator = get_direction_orchestrator()
                tick_count = getattr(self, '_idle_eval_count', 0)  # Use idle_eval_count as tick proxy
                orchestrator.record_direction_outcome(was_profitable=is_win, tick_count=tick_count)
            except Exception as cb_err:
                logger.debug(f"Circuit breaker recording error: {cb_err}")
            
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
            
            # =========================================================================
            # 🔥 FEB06: POST-TRADE ADAPTIVE COOLDOWN
            # =========================================================================
            # Aplica cooldown adaptativo según resultado del trade:
            # - WIN (+0.1R+): 30s - puede seguir operando rápido
            # - BREAKEVEN: 60s
            # - LOSS: 6 minutos - penalización para evitar tilt
            # =========================================================================
            if hasattr(self, 'cooldown_manager'):
                self.cooldown_manager.activate_post_trade_cooldown(
                    current_time=market_time,
                    pnl_r=pnl_r,
                    exit_reason=reason
                )
            
            # =========================================================================
            # 🔥 FIX #11: BREAKEVEN TRADES DON'T COUNT (Feb 02, 2026)
            # =========================================================================
            # If trade closed with |PnL| < 0.15R, it's a "breakeven" that didn't
            # develop. Don't punish the session limit for it - refund the slot.
            # 
            # Example: Entry @ $77,144 → Exit @ $77,152 = -0.04R (breakeven)
            # This shouldn't consume a session trade slot.
            # =========================================================================
            BREAKEVEN_THRESHOLD_R = 0.15
            if abs(pnl_r) < BREAKEVEN_THRESHOLD_R:
                # Refund session trade slot
                current_session = get_session_name(market_time.hour, market_time.minute, market_time.weekday())
                if current_session in self._session_trade_counts and self._session_trade_counts[current_session] > 0:
                    self._session_trade_counts[current_session] -= 1
                    logger.warning(
                        f"🔄 BREAKEVEN REFUND: Trade closed at {pnl_r:.2f}R (< {BREAKEVEN_THRESHOLD_R}R threshold) | "
                        f"Session {current_session}: slot refunded → {self._session_trade_counts[current_session]} trades"
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
    # 🔧 REFACTORED (Jan 27, 2026): All filter wrappers removed - use filter_checks directly
    # All bias/trend analysis moved to src/application/live/market_analysis.py
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
        
        # 🔥 FIX #56: Get context for structure gate overrides
        expansion_mode_active = self._current_trade_tags.get('expansion_mode_activated', False) if hasattr(self, '_current_trade_tags') else False
        effective_adx = 0.0
        if hasattr(self, '_current_trade_tags'):
            effective_adx = self._current_trade_tags.get('effective_adx', 0) or self._current_trade_tags.get('htf_adx', 0)
        
        # 🔥 FIX #61: Get bias inversion status
        bias_inverted = self._current_trade_tags.get('bias_inverted', False) if hasattr(self, '_current_trade_tags') else False
        
        # 🔥 FIX #63: Get bias invalidation status (opposite direction forbidden)
        bias_invalidated = self._current_trade_tags.get('bias_invalidated', False) if hasattr(self, '_current_trade_tags') else False
        
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
            expansion_mode_active=expansion_mode_active,  # 🔥 FIX #56
            strong_trend_adx=effective_adx,               # 🔥 FIX #56
            bias_inverted=bias_inverted,                  # 🔥 FIX #61
            bias_invalidated=bias_invalidated,            # 🔥 FIX #63
        )
        
        # Copy trade tags from scorer to signal engine for backwards compatibility
        scorer_tags = self._entry_scorer.trade_tags
        
        # 🧠 Apply TrendIntelligence score adjustment (Feb 06, 2026)
        ti_score_adj = getattr(self, '_ti_score_adjustment', 0.0)
        adjusted_score = scorer_tags.score + ti_score_adj
        
        self._current_trade_tags = {
            'score': adjusted_score,
            'score_ti_adjustment': ti_score_adj,
            'mode': scorer_tags.mode,
            'htf_aligned': scorer_tags.htf_aligned,
            'ltf_clean': scorer_tags.ltf_clean,
            'bos_confirmed': scorer_tags.bos_confirmed,
            'bos_quality': scorer_tags.bos_quality,  # 🔥 NEW (Feb 03): For dynamic trailing
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
    
    # 🔥 REMOVED (Feb 04, 2026): Dead code - 6 unused filter wrappers deleted
    # - _check_session_filter (was always True)
    # - _check_trend_filter (delegated, never called)
    # - _check_momentum_filter (~100 lines, never called)
    # - _check_volatility_expansion (delegated, never called)
    # - _check_anti_reverso (delegated, never called)
    # - _check_volume_confirmation (delegated, never called)
    
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
        """BOS detection - delegates to position_manager with edge-triggered logging
        
        🔧 FIX #127 (Feb 09, 2026): Use unified BOS detection from position_manager
        """
        if not candles_5m or len(candles_5m) < 5:
            return False
            
        df = pd.DataFrame(candles_5m)
        has_bos, bos_level = _check_bos_unified(df, direction)
        
        # Edge-triggered logging (only log once per new BOS level)
        if has_bos and bos_level != 0.0:
            if self._last_bos_level != bos_level or self._last_bos_direction != direction:
                icon = "📉" if direction == 'LONG' else "📈"
                bos_type = "LL" if direction == 'LONG' else "HH"
                logger.info(f"{icon} BOS detectado ({direction}): {bos_type} @ {bos_level:.2f}")
                self._last_bos_level = bos_level
                self._last_bos_direction = direction
        
        return has_bos
    
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
    
    # 🔥 REMOVED (Feb 04, 2026): get_important_levels_info - never called
    
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
                    # 🔥 FIX (Feb 4, 2026): Actively recover from desync, don't just log
                    # This prevents MFE/trailing/add-to-winner using stale or null state
                    logger.warning("⚠️ Position store has position but risk engine doesn't - FORCING RECOVERY")
                    try:
                        # Force state machine to MANAGE so recovery runs
                        if self.state_machine.state != BotState.MANAGE:
                            self.state_machine.transition_to(BotState.MANAGE, "Forced recovery from position desync")
                        # The next tick's _on_new_candle_actions will handle position recovery
                        # But also set a flag to ensure reconciliation happens immediately
                        self._force_position_recovery = True
                    except Exception as e:
                        logger.error(f"Failed to force recovery: {e}")
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

