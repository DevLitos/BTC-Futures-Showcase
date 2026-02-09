"""
Missed Opportunity Tracker v2.0 - REAL Trade Simulation

"You need to know: How many REAL trades did I NOT take?"

🔥 COMPLETE REWRITE (Feb 07, 2026):

PROBLEMA ANTERIOR:
- Usaba precio de bloqueo como "entry" → FALSO
- MFE > MAE como criterio de "buen trade" → HINDSIGHT BIAS
- Ventana fija de 60 min → ARBITRARIO
- No verificaba invalidación estructural → FAKE MISSED
- No simulaba ejecución real → OPTIMISMO ESTRUCTURAL

SOLUCIÓN:
1. SETUP-BASED: Usa origin_price, stop_level, invalidation_level del EDGE
2. PATH SIMULATION: ¿Tocó entry? ¿Tocó stop primero? ¿Se invalidó?
3. DYNAMIC TTL: Lifetime basado en tipo de edge (no fijo)
4. REAL OUTCOME: winner/loser/dead/unresolved (no MFE)
5. EXECUTION MODEL: Entry solo si precio CRUZA nivel (no cualquier precio)

Purpose:
- Detectar si filtros bloquean trades REALES (no inventados)
- Cuantificar costo de oportunidad REAL
- Distinguir "DODGED LOSER" vs "REAL MISSED" correctamente
"""

import logging
from datetime import datetime, UTC, timedelta
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from collections import deque
from enum import Enum
import json

logger = logging.getLogger(__name__)


# =============================================================================
# 📊 OUTCOME TYPES - Estados finales de un setup bloqueado
# =============================================================================
class SetupOutcome(Enum):
    """
    Resultado REAL del setup (no hindsight).
    
    Se determina por PATH del precio, no por MFE/MAE.
    """
    PENDING = "PENDING"              # Aún tracking
    ENTRY_NEVER_HIT = "ENTRY_NEVER_HIT"  # Entry level nunca tocado → no era trade
    INVALIDATED = "INVALIDATED"      # Estructura invalidada antes de TP/SL
    WINNER = "WINNER"                # TP tocado primero
    LOSER = "LOSER"                  # SL tocado primero
    EXPIRED = "EXPIRED"              # TTL expirado sin resolución


class SetupTimingType(Enum):
    """
    Timing de detección vs formación del setup.
    
    EARLY = Detectamos antes de que precio llegue a zona ideal
    ON_TIME = Detectamos cuando precio está en zona
    LATE = Detectamos después de que precio ya movió significativamente
    """
    EARLY = "EARLY"        # < 20% del move inicial
    ON_TIME = "ON_TIME"    # 20-50% del move
    LATE = "LATE"          # 50-80% del move
    VERY_LATE = "VERY_LATE"  # > 80% del move (persiguiendo)
    UNKNOWN = "UNKNOWN"


# Legacy compatibility enum
class SignalTimingCategory(Enum):
    """🔥 LEGACY: Kept for backwards compatibility."""
    EARLY = "early"
    ON_TIME = "on_time"
    LATE = "late"
    VERY_LATE = "very_late"
    UNKNOWN = "unknown"


# =============================================================================
# 📊 SETUP CONFIG - TTL por tipo de edge
# =============================================================================
@dataclass
class SetupTTLConfig:
    """
    Time-to-live por tipo de edge.
    
    Basado en el horizonte natural de cada tipo:
    - DISPLACEMENT: Movimiento rápido, resolución rápida
    - RANGE: Mean reversion, puede tardar más
    - TREND: Continuación, horizonte medio
    - BREAKOUT: Post-compresión, resolución rápida
    """
    DISPLACEMENT_MINUTES: int = 15   # Resolve in 15 min or dead
    RANGE_MINUTES: int = 45          # Can take up to 45 min
    TREND_MINUTES: int = 30          # 30 min window
    BREAKOUT_MINUTES: int = 20       # 20 min for breakout
    DEFAULT_MINUTES: int = 30        # Fallback
    
    @classmethod
    def get_ttl(cls, edge_type: str) -> int:
        """Get TTL in minutes for edge type."""
        mapping = {
            'DISPLACEMENT': cls.DISPLACEMENT_MINUTES,
            'RANGE': cls.RANGE_MINUTES,
            'TREND': cls.TREND_MINUTES,
            'BREAKOUT': cls.BREAKOUT_MINUTES,
        }
        return mapping.get(edge_type, cls.DEFAULT_MINUTES)


# =============================================================================
# 📊 BLOCKED SETUP - Un setup que fue bloqueado
# =============================================================================
@dataclass
class BlockedSetup:
    """
    Un setup de trading que fue BLOQUEADO.
    
    Contiene TODO lo necesario para simular el trade:
    - Niveles del setup (no precio arbitrario)
    - Reglas de invalidación
    - TTL natural del edge
    """
    # ========== IDENTIDAD ==========
    timestamp: datetime
    edge_type: str  # 'DISPLACEMENT', 'RANGE', 'TREND', 'BREAKOUT'
    direction: str  # 'LONG' or 'SHORT'
    block_reason: str
    block_category: str
    
    # ========== SETUP LEVELS (del EDGE, no del precio actual) ==========
    origin_price: float  # Donde empezó el setup
    entry_level: float   # Nivel de entrada ideal (puede ser != current price)
    stop_level: float    # SL estructural
    tp_level: float      # TP calculado
    invalidation_level: float  # Si cruza esto → setup MUERTO
    
    # ========== DETECTION TIMING ==========
    detection_price: float  # Precio cuando detectamos/bloqueamos
    move_pct_at_detection: float = 0.0  # % del move que ya ocurrió
    timing_type: SetupTimingType = SetupTimingType.UNKNOWN
    
    # ========== CONTEXT ==========
    atr: float = 0.0
    score: float = 0.0
    
    # ========== SIMULATION STATE ==========
    entry_touched: bool = False      # ¿Precio llegó a entry level?
    entry_price: float = 0.0         # Precio exacto de "fill" simulado
    entry_time: Optional[datetime] = None
    
    stop_touched: bool = False
    tp_touched: bool = False
    invalidation_touched: bool = False
    
    high_since_entry: float = 0.0    # Para tracking (no para decisión)
    low_since_entry: float = 0.0
    
    # ========== OUTCOME ==========
    outcome: SetupOutcome = SetupOutcome.PENDING
    outcome_price: float = 0.0
    outcome_time: Optional[datetime] = None
    outcome_reason: str = ""
    
    # ========== TTL ==========
    ttl_minutes: int = 30
    expires_at: Optional[datetime] = None
    
    def __post_init__(self):
        """Set expiration time based on edge type."""
        self.ttl_minutes = SetupTTLConfig.get_ttl(self.edge_type)
        self.expires_at = self.timestamp + timedelta(minutes=self.ttl_minutes)
    
    @property
    def risk_r(self) -> float:
        """Risk in R terms."""
        if self.atr <= 0:
            return 0.0
        return abs(self.entry_level - self.stop_level) / self.atr
    
    @property
    def reward_r(self) -> float:
        """Reward in R terms."""
        if self.atr <= 0:
            return 0.0
        return abs(self.tp_level - self.entry_level) / self.atr
    
    @property
    def rr_ratio(self) -> float:
        """Risk:Reward ratio."""
        if self.risk_r <= 0:
            return 0.0
        return self.reward_r / self.risk_r
    
    @property
    def is_real_missed(self) -> bool:
        """
        ¿Fue un REAL missed? 
        
        Criterios:
        1. El trade era WINNER (TP tocado primero)
        2. Detectamos EARLY/ON_TIME (no persiguiendo)
        3. Entry fue tocado (no imaginario)
        """
        return (
            self.outcome == SetupOutcome.WINNER and
            self.timing_type in [SetupTimingType.EARLY, SetupTimingType.ON_TIME] and
            self.entry_touched
        )
    
    @property
    def is_dodged_loser(self) -> bool:
        """¿Esquivamos un loser?"""
        return (
            self.outcome == SetupOutcome.LOSER and
            self.entry_touched  # Only counts if entry was hit
        )
    
    @property
    def is_dead_setup(self) -> bool:
        """¿Era un setup muerto (invalidado)?"""
        return self.outcome == SetupOutcome.INVALIDATED
    
    @property
    def is_phantom(self) -> bool:
        """¿Era un trade fantasma (entry nunca tocado)?"""
        return self.outcome == SetupOutcome.ENTRY_NEVER_HIT
    
    @property
    def classification(self) -> str:
        """Clasificación final del setup bloqueado."""
        if not self.outcome or self.outcome == SetupOutcome.PENDING:
            return "PENDING"
        
        if self.outcome == SetupOutcome.ENTRY_NEVER_HIT:
            return "PHANTOM_TRADE"  # Trade que nunca existió
        
        if self.outcome == SetupOutcome.INVALIDATED:
            return "DEAD_SETUP"  # Setup murió estructuralmente
        
        if self.outcome == SetupOutcome.EXPIRED:
            return "STALE"  # Expiró sin resolución
        
        if self.outcome == SetupOutcome.WINNER:
            if self.timing_type in [SetupTimingType.EARLY, SetupTimingType.ON_TIME]:
                return "REAL_MISSED"  # Este SÍ era un trade real
            else:
                return "LATE_DETECTION_WINNER"  # Ganó pero lo detectamos tarde
        
        if self.outcome == SetupOutcome.LOSER:
            return "DODGED_LOSER"  # Correcto bloquear
        
        return "UNKNOWN"
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for persistence."""
        return {
            'timestamp': self.timestamp.isoformat(),
            'edge_type': self.edge_type,
            'direction': self.direction,
            'block_reason': self.block_reason,
            'block_category': self.block_category,
            # Levels
            'origin_price': self.origin_price,
            'entry_level': self.entry_level,
            'stop_level': self.stop_level,
            'tp_level': self.tp_level,
            'invalidation_level': self.invalidation_level,
            # Detection
            'detection_price': self.detection_price,
            'move_pct_at_detection': self.move_pct_at_detection,
            'timing_type': self.timing_type.value,
            # Context
            'atr': self.atr,
            'score': self.score,
            'risk_r': self.risk_r,
            'reward_r': self.reward_r,
            'rr_ratio': self.rr_ratio,
            # Simulation
            'entry_touched': self.entry_touched,
            'entry_price': self.entry_price,
            # Outcome
            'outcome': self.outcome.value,
            'outcome_price': self.outcome_price,
            'outcome_reason': self.outcome_reason,
            'classification': self.classification,
            'is_real_missed': self.is_real_missed,
            'is_dodged_loser': self.is_dodged_loser,
            'ttl_minutes': self.ttl_minutes,
        }


# =============================================================================
# 📊 LEGACY DATACLASS - For backwards compatibility
# =============================================================================
@dataclass
class MissedOpportunity:
    """
    🚨 LEGACY: Kept for backwards compatibility only.
    Use BlockedSetup for new code.
    """
    timestamp: datetime
    signal_direction: str
    hypothetical_entry: float
    block_reason: str
    block_category: str
    signal_score: float = 0.0
    atr_at_signal: float = 0.0
    timing_category: SignalTimingCategory = SignalTimingCategory.UNKNOWN
    move_at_detection_pct: float = 0.0
    setup_price: float = 0.0
    price_5min_later: Optional[float] = None
    price_15min_later: Optional[float] = None
    price_1h_later: Optional[float] = None
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    hypothetical_r: float = 0.0
    resolved: bool = False
    
    @property
    def was_good_trade(self) -> bool:
        return False  # Legacy - always False in v2
    
    @property
    def is_real_missed(self) -> bool:
        return False  # Legacy
    
    @property
    def is_fake_missed(self) -> bool:
        return False  # Legacy
    
    @property
    def missed_classification(self) -> str:
        return "LEGACY_UNSUPPORTED"
    
    @property
    def hypothetical_r_at_mfe(self) -> float:
        return 0.0
    
    def to_dict(self) -> Dict:
        return {'legacy': True, 'timestamp': self.timestamp.isoformat()}


# =============================================================================
# 📊 TRACKER V2 - Simulación real de setups bloqueados
# =============================================================================
class MissedOpportunityTracker:
    """
    Track blocked trading setups with REAL outcome simulation.
    
    V2 CHANGES:
    1. Usa niveles del setup (no precio arbitrario)
    2. Simula path del precio (entry→SL/TP/invalidation)
    3. TTL dinámico por tipo de edge
    4. Outcome real (no MFE/MAE hindsight)
    5. Solo cuenta REAL MISSED (entry hit + winner + early detection)
    
    Usage:
        tracker = MissedOpportunityTracker()
        
        # When a signal is blocked, pass the SETUP levels:
        tracker.log_blocked_setup(
            edge_type='DISPLACEMENT',
            direction='SHORT',
            block_reason='chase_penalty',
            block_category='filter',
            origin_price=100000.0,
            entry_level=99800.0,
            stop_level=100100.0,
            tp_level=99200.0,
            invalidation_level=100050.0,
            detection_price=99750.0,
            atr=100.0
        )
        
        # On every tick:
        tracker.update(current_price=99500.0)
        
        # Get real statistics:
        stats = tracker.get_statistics()
    """
    
    MAX_PENDING = 50  # Max setups tracking simultaneously
    TRACKING_DURATION_MINUTES = 60  # Legacy compat
    
    def __init__(self, persist_path: Optional[str] = None):
        self._pending: deque[BlockedSetup] = deque(maxlen=self.MAX_PENDING)
        self._completed: List[BlockedSetup] = []
        self._persist_path = persist_path
        
        # 🔥 Throttle para logs repetitivos
        self._last_blocked_reason: str = ""
        self._same_reason_count: int = 0
        
        # ========== STATISTICS ==========
        self._total_blocked = 0
        
        # Outcome counters
        self._real_missed = 0        # WINNER + early detection + entry hit
        self._late_detection_winners = 0  # WINNER but detected late
        self._dodged_losers = 0      # LOSER with entry hit
        self._dead_setups = 0        # INVALIDATED
        self._phantom_trades = 0     # Entry never hit
        self._stale = 0              # Expired without resolution
        
        # Legacy compat
        self._good_trades_missed = 0
        self._bad_trades_avoided = 0
        
        # By block reason
        self._block_counts: Dict[str, int] = {}
        self._block_real_missed: Dict[str, int] = {}
        self._block_good_missed: Dict[str, int] = {}  # Legacy alias
        
        # Legacy timing stats
        self._real_missed_early = 0
        self._real_missed_ontime = 0
        self._fake_missed_late = 0
        self._fake_missed_chase = 0
        
        logger.info("MissedOpportunityTracker v2.0 initialized (REAL simulation)")
    
    # =========================================================================
    # 📊 LEGACY COMPATIBILITY - Redirect old calls to new format
    # =========================================================================
    def log_blocked_signal(
        self,
        direction: str | None,
        price: float,
        block_reason: str,
        block_category: str,
        score: float = 0.0,
        atr: float = 0.0,
        # Legacy timing params (ignored in v2)
        timing_category: Any = None,
        move_at_detection_pct: float = 0.0,
        setup_price: float = 0.0,
        # NEW v2 params (required for real simulation)
        origin_price: float = 0.0,
        entry_level: float = 0.0,
        stop_level: float = 0.0,
        tp_level: float = 0.0,
        invalidation_level: float = 0.0,
        edge_type: str = 'UNKNOWN'
    ):
        """
        LEGACY COMPATIBILITY WRAPPER.
        
        Old code calls this with just price.
        New code should call log_blocked_setup() directly.
        
        If proper levels aren't provided, we log but mark as INCOMPLETE
        (won't generate false "missed" stats).
        """
        # ========== PROTECTION CATEGORIES (NOT missed opportunities) ==========
        PROTECTION_CATEGORIES = {
            'delta_contradict',
            'chase_penalty', 
            'obstinacy',
            'quality_gate',
            'location_filter',
            'location_extreme',
            'forbidden_direction',
            'bias_flip',
            'direction_lock',
            'warmup',
        }
        
        if block_category in PROTECTION_CATEGORIES:
            logger.debug(
                f"[PROTECTED] {direction} @ {price:.2f} | "
                f"{block_category}: {block_reason}"
            )
            return
        
        # Check if we have real setup levels
        has_real_levels = (
            origin_price > 0 and 
            stop_level > 0 and 
            invalidation_level > 0
        )
        
        if has_real_levels:
            # New-style call with proper levels
            self.log_blocked_setup(
                edge_type=edge_type,
                direction=direction or 'UNKNOWN',
                block_reason=block_reason,
                block_category=block_category,
                origin_price=origin_price,
                entry_level=entry_level if entry_level > 0 else price,
                stop_level=stop_level,
                tp_level=tp_level,
                invalidation_level=invalidation_level,
                detection_price=price,
                atr=atr,
                score=score
            )
        else:
            # Legacy call - log but don't track (incomplete data)
            # 🔥 Throttle: solo log si cambia el reason o cada 60 bloqueados
            if block_reason != self._last_blocked_reason:
                if self._same_reason_count > 1:
                    logger.info(f"📊 (suppressed {self._same_reason_count} similar blocks)")
                self._same_reason_count = 1
                self._last_blocked_reason = block_reason
                logger.info(
                    f"📊 BLOCKED: {direction} @ {price:.2f} | "
                    f"{block_category}: {block_reason}"
                )
            else:
                self._same_reason_count += 1
                # Log resumen cada 60 bloques del mismo tipo
                if self._same_reason_count % 60 == 0:
                    logger.info(
                        f"📊 BLOCKED x{self._same_reason_count}: {direction} | "
                        f"{block_category}: {block_reason}"
                    )
            self._total_blocked += 1
            self._block_counts[block_reason] = self._block_counts.get(block_reason, 0) + 1
    
    # =========================================================================
    # 📊 NEW V2 API - Log blocked setup with REAL levels
    # =========================================================================
    def log_blocked_setup(
        self,
        edge_type: str,
        direction: str,
        block_reason: str,
        block_category: str,
        origin_price: float,
        entry_level: float,
        stop_level: float,
        tp_level: float,
        invalidation_level: float,
        detection_price: float,
        atr: float = 0.0,
        score: float = 0.0
    ):
        """
        Log a blocked setup with REAL levels for simulation.
        
        This is the correct way to log missed opportunities.
        """
        # Calculate timing
        timing_type, move_pct = self._calculate_timing(
            origin_price=origin_price,
            entry_level=entry_level,
            detection_price=detection_price,
            direction=direction
        )
        
        setup = BlockedSetup(
            timestamp=datetime.now(UTC),
            edge_type=edge_type,
            direction=direction.upper(),
            block_reason=block_reason,
            block_category=block_category,
            origin_price=origin_price,
            entry_level=entry_level,
            stop_level=stop_level,
            tp_level=tp_level,
            invalidation_level=invalidation_level,
            detection_price=detection_price,
            move_pct_at_detection=move_pct,
            timing_type=timing_type,
            atr=atr,
            score=score,
        )
        
        # Initialize tracking highs/lows
        setup.high_since_entry = detection_price
        setup.low_since_entry = detection_price
        
        self._pending.append(setup)
        self._total_blocked += 1
        self._block_counts[block_reason] = self._block_counts.get(block_reason, 0) + 1
        
        logger.info(
            f"📊 BLOCKED SETUP: {edge_type} {direction} | "
            f"Entry={entry_level:.0f} SL={stop_level:.0f} TP={tp_level:.0f} | "
            f"Timing={timing_type.value} ({move_pct:.0f}% move) | "
            f"{block_category}: {block_reason}"
        )
    
    def _calculate_timing(
        self,
        origin_price: float,
        entry_level: float,
        detection_price: float,
        direction: str
    ) -> tuple[SetupTimingType, float]:
        """
        Calculate timing of detection.
        
        Returns (timing_type, move_percentage)
        """
        if origin_price <= 0 or entry_level <= 0:
            return SetupTimingType.UNKNOWN, 0.0
        
        total_move = abs(entry_level - origin_price)
        if total_move <= 0:
            return SetupTimingType.UNKNOWN, 0.0
        
        # How much has price moved from origin towards entry?
        if direction.upper() == 'LONG':
            # LONG: origin is LOW, entry is higher
            # detection_price closer to origin = early
            current_move = detection_price - origin_price
        else:
            # SHORT: origin is HIGH, entry is lower
            # detection_price closer to origin = early
            current_move = origin_price - detection_price
        
        move_pct = (current_move / total_move) * 100 if total_move > 0 else 0
        move_pct = max(0, min(100, move_pct))  # Clamp to 0-100
        
        if move_pct < 20:
            return SetupTimingType.EARLY, move_pct
        elif move_pct < 50:
            return SetupTimingType.ON_TIME, move_pct
        elif move_pct < 80:
            return SetupTimingType.LATE, move_pct
        else:
            return SetupTimingType.VERY_LATE, move_pct
    
    # =========================================================================
    # 📊 UPDATE - Simulate price path on each tick
    # =========================================================================
    def update(self, current_price: float, current_time: datetime | None = None):
        """
        Update all pending setups with new price.
        
        SIMULATION LOGIC:
        1. Check if expired (TTL)
        2. Check invalidation FIRST (structure died → DEAD)
        3. Check entry touched (if not yet)
        4. If entry touched: check SL/TP
        5. First to touch wins
        """
        if current_time is None:
            current_time = datetime.now(UTC)
        
        resolved_indices = []
        
        for i, setup in enumerate(self._pending):
            if setup.outcome != SetupOutcome.PENDING:
                continue
            
            # ========== CHECK EXPIRATION ==========
            if setup.expires_at and current_time >= setup.expires_at:
                if setup.entry_touched:
                    # Entry was hit but no resolution
                    self._resolve_setup(setup, SetupOutcome.EXPIRED, current_price, 
                                       "TTL expired (entry hit but no SL/TP)")
                else:
                    # Entry never hit
                    self._resolve_setup(setup, SetupOutcome.ENTRY_NEVER_HIT, current_price,
                                       "TTL expired, entry never touched")
                resolved_indices.append(i)
                continue
            
            # ========== CHECK INVALIDATION (before entry check) ==========
            if self._check_invalidation(setup, current_price):
                self._resolve_setup(setup, SetupOutcome.INVALIDATED, current_price,
                                   f"Invalidation hit @ {current_price:.0f}")
                resolved_indices.append(i)
                continue
            
            # ========== CHECK ENTRY TOUCH ==========
            if not setup.entry_touched:
                if self._check_entry_touch(setup, current_price):
                    setup.entry_touched = True
                    setup.entry_price = current_price
                    setup.entry_time = current_time
                    setup.high_since_entry = current_price
                    setup.low_since_entry = current_price
                    logger.debug(f"📊 Setup entry touched: {setup.direction} @ {current_price:.0f}")
            
            # ========== IF ENTRY TOUCHED: CHECK SL/TP ==========
            if setup.entry_touched:
                # Update highs/lows
                setup.high_since_entry = max(setup.high_since_entry, current_price)
                setup.low_since_entry = min(setup.low_since_entry, current_price)
                
                # Check STOP LOSS
                if self._check_stop_hit(setup, current_price):
                    setup.stop_touched = True
                    self._resolve_setup(setup, SetupOutcome.LOSER, current_price,
                                       f"Stop hit @ {current_price:.0f}")
                    resolved_indices.append(i)
                    continue
                
                # Check TAKE PROFIT
                if self._check_tp_hit(setup, current_price):
                    setup.tp_touched = True
                    self._resolve_setup(setup, SetupOutcome.WINNER, current_price,
                                       f"TP hit @ {current_price:.0f}")
                    resolved_indices.append(i)
                    continue
        
        # Remove resolved from pending
        if resolved_indices:
            resolved_set = set(resolved_indices)
            new_pending = deque(maxlen=self.MAX_PENDING)
            for i, setup in enumerate(self._pending):
                if i not in resolved_set:
                    new_pending.append(setup)
            self._pending = new_pending
    
    # Alias for legacy code
    def update_pending(self, current_price: float, current_time: datetime | None = None):
        """Legacy alias for update()."""
        self.update(current_price, current_time)
    
    def _check_invalidation(self, setup: BlockedSetup, price: float) -> bool:
        """Check if price crossed invalidation level."""
        if setup.invalidation_level <= 0:
            return False
        
        if setup.direction == 'LONG':
            # LONG invalidates if price goes BELOW invalidation
            return price < setup.invalidation_level
        else:
            # SHORT invalidates if price goes ABOVE invalidation
            return price > setup.invalidation_level
    
    def _check_entry_touch(self, setup: BlockedSetup, price: float) -> bool:
        """Check if price reached entry level."""
        if setup.entry_level <= 0:
            return False
        
        if setup.direction == 'LONG':
            # LONG: Entry touched if price drops to or below entry level
            return price <= setup.entry_level
        else:
            # SHORT: Entry touched if price rises to or above entry level
            return price >= setup.entry_level
    
    def _check_stop_hit(self, setup: BlockedSetup, price: float) -> bool:
        """Check if stop loss was hit."""
        if setup.stop_level <= 0:
            return False
        
        if setup.direction == 'LONG':
            # LONG SL is below entry
            return price <= setup.stop_level
        else:
            # SHORT SL is above entry
            return price >= setup.stop_level
    
    def _check_tp_hit(self, setup: BlockedSetup, price: float) -> bool:
        """Check if take profit was hit."""
        if setup.tp_level <= 0:
            return False
        
        if setup.direction == 'LONG':
            # LONG TP is above entry
            return price >= setup.tp_level
        else:
            # SHORT TP is below entry
            return price <= setup.tp_level
    
    def _resolve_setup(
        self, 
        setup: BlockedSetup, 
        outcome: SetupOutcome, 
        price: float,
        reason: str
    ):
        """Resolve a setup with final outcome."""
        setup.outcome = outcome
        setup.outcome_price = price
        setup.outcome_time = datetime.now(UTC)
        setup.outcome_reason = reason
        
        self._completed.append(setup)
        
        # Update statistics
        classification = setup.classification
        
        if classification == "REAL_MISSED":
            self._real_missed += 1
            self._good_trades_missed += 1  # Legacy compat
            self._block_real_missed[setup.block_reason] = \
                self._block_real_missed.get(setup.block_reason, 0) + 1
            self._block_good_missed[setup.block_reason] = \
                self._block_good_missed.get(setup.block_reason, 0) + 1
            
            # Legacy timing stats
            if setup.timing_type == SetupTimingType.EARLY:
                self._real_missed_early += 1
            elif setup.timing_type == SetupTimingType.ON_TIME:
                self._real_missed_ontime += 1
            
            logger.warning(
                f"❌ REAL MISSED: {setup.direction} {setup.edge_type} | "
                f"Entry={setup.entry_level:.0f} → TP={setup.tp_level:.0f} | "
                f"Blocked by: {setup.block_reason} | {reason}"
            )
        elif classification == "LATE_DETECTION_WINNER":
            self._late_detection_winners += 1
            
            # Legacy stats
            if setup.timing_type == SetupTimingType.LATE:
                self._fake_missed_late += 1
            else:
                self._fake_missed_chase += 1
            
            logger.info(
                f"⏰ LATE DETECTION WINNER: {setup.direction} | "
                f"Detected after {setup.move_pct_at_detection:.0f}% move | "
                f"Not a real miss (was chasing)"
            )
        elif classification == "DODGED_LOSER":
            self._dodged_losers += 1
            self._bad_trades_avoided += 1  # Legacy compat
            logger.info(
                f"✅ DODGED LOSER: {setup.direction} | "
                f"Entry={setup.entry_level:.0f} → SL hit | "
                f"Blocked by: {setup.block_reason}"
            )
        elif classification == "DEAD_SETUP":
            self._dead_setups += 1
            self._bad_trades_avoided += 1  # Legacy compat
            logger.info(
                f"💀 DEAD SETUP: {setup.direction} | "
                f"Invalidated @ {price:.0f} | Structure died"
            )
        elif classification == "PHANTOM_TRADE":
            self._phantom_trades += 1
            logger.debug(
                f"👻 PHANTOM: {setup.direction} | "
                f"Entry {setup.entry_level:.0f} never touched"
            )
        elif classification == "STALE":
            self._stale += 1
            logger.debug(f"⌛ STALE: {setup.direction} | Expired without resolution")
        
        # Persist if configured
        if self._persist_path:
            self._persist_setup(setup)
    
    def _persist_setup(self, setup: BlockedSetup):
        """Append setup to file."""
        if self._persist_path is None:
            return
        try:
            with open(self._persist_path, 'a') as f:
                f.write(json.dumps(setup.to_dict()) + '\n')
        except Exception as e:
            logger.error(f"Failed to persist setup: {e}")
    
    # =========================================================================
    # 📊 STATISTICS
    # =========================================================================
    def get_statistics(self) -> Dict[str, Any]:
        """Get comprehensive statistics."""
        total_resolved = (
            self._real_missed + 
            self._late_detection_winners +
            self._dodged_losers + 
            self._dead_setups + 
            self._phantom_trades +
            self._stale
        )
        
        # Filter effectiveness
        filter_stats = {}
        for reason in self._block_counts:
            total = self._block_counts[reason]
            real_missed = self._block_real_missed.get(reason, 0)
            
            filter_stats[reason] = {
                'total_blocks': total,
                'real_missed': real_missed,
                'good_trades_missed': real_missed,  # Legacy alias
                'false_positive_rate': real_missed / total if total > 0 else 0.0
            }
        
        # Legacy timing analysis
        real_missed = self._real_missed_early + self._real_missed_ontime
        fake_missed = self._fake_missed_late + self._fake_missed_chase
        total_missed = real_missed + fake_missed
        
        return {
            'total_blocked': self._total_blocked,
            'pending_tracking': len(self._pending),
            'resolved': total_resolved,
            # Real outcomes (v2)
            'real_missed': self._real_missed,
            'late_detection_winners': self._late_detection_winners,
            'dodged_losers': self._dodged_losers,
            'dead_setups': self._dead_setups,
            'phantom_trades': self._phantom_trades,
            'stale': self._stale,
            # Legacy compat
            'good_trades_missed': self._good_trades_missed,
            'bad_trades_avoided': self._bad_trades_avoided,
            # Rates
            'real_miss_rate': self._real_missed / total_resolved if total_resolved > 0 else 0.0,
            'miss_rate': self._good_trades_missed / total_resolved if total_resolved > 0 else 0.0,
            'dodged_rate': self._dodged_losers / total_resolved if total_resolved > 0 else 0.0,
            'phantom_rate': self._phantom_trades / total_resolved if total_resolved > 0 else 0.0,
            # Filter analysis
            'filter_effectiveness': filter_stats,
            # Legacy timing analysis
            'timing_analysis': {
                'real_missed': real_missed,
                'real_missed_early': self._real_missed_early,
                'real_missed_ontime': self._real_missed_ontime,
                'fake_missed': fake_missed,
                'fake_missed_late': self._fake_missed_late,
                'fake_missed_chase': self._fake_missed_chase,
                'real_missed_pct': real_missed / total_missed if total_missed > 0 else 0.0,
                'fake_missed_pct': fake_missed / total_missed if total_missed > 0 else 0.0,
            }
        }
    
    def get_worst_filters(self, top_n: int = 5) -> List[Dict]:
        """
        Get filters with highest false positive rate (blocking REAL trades).
        """
        filter_stats = []
        
        for reason in self._block_counts:
            total = self._block_counts[reason]
            real_missed = self._block_real_missed.get(reason, 0)
            
            if total >= 3:  # Minimum sample
                filter_stats.append({
                    'filter': reason,
                    'total_blocks': total,
                    'real_missed': real_missed,
                    'good_missed': real_missed,  # Legacy alias
                    'false_positive_rate': real_missed / total
                })
        
        filter_stats.sort(key=lambda x: x['false_positive_rate'], reverse=True)
        return filter_stats[:top_n]
    
    def log_summary(self):
        """Log a summary of missed opportunities."""
        stats = self.get_statistics()
        timing = stats.get('timing_analysis', {})
        
        logger.info("=" * 60)
        logger.info("📊 MISSED OPPORTUNITY SUMMARY (v2.0)")
        logger.info("=" * 60)
        logger.info(f"Total blocked: {stats['total_blocked']}")
        logger.info(f"Currently tracking: {stats['pending_tracking']}")
        logger.info(f"Resolved: {stats['resolved']}")
        logger.info("")
        logger.info("📊 OUTCOMES:")
        logger.info(f"  ❌ REAL MISSED (true opportunities): {stats['real_missed']}")
        logger.info(f"  ⏰ Late detection winners: {stats['late_detection_winners']}")
        logger.info(f"  ✅ DODGED LOSERS: {stats['dodged_losers']}")
        logger.info(f"  💀 Dead setups (invalidated): {stats['dead_setups']}")
        logger.info(f"  👻 Phantom trades (entry never hit): {stats['phantom_trades']}")
        logger.info(f"  ⌛ Stale (expired): {stats['stale']}")
        logger.info("")
        logger.info(f"Real miss rate: {stats['real_miss_rate']:.1%}")
        logger.info(f"Phantom rate: {stats['phantom_rate']:.1%}")
        
        # Legacy timing analysis
        if timing:
            logger.info("")
            logger.info("📊 TIMING ANALYSIS (Real vs Fake Missed):")
            logger.info(f"  🎯 REAL MISSED (detected early):")
            logger.info(f"     Early: {timing.get('real_missed_early', 0)}")
            logger.info(f"     On-time: {timing.get('real_missed_ontime', 0)}")
            logger.info(f"  ⏰ FAKE MISSED (detected late - not real opportunities):")
            logger.info(f"     Late: {timing.get('fake_missed_late', 0)}")
            logger.info(f"     Chase: {timing.get('fake_missed_chase', 0)}")
        
        worst = self.get_worst_filters(3)
        if worst:
            logger.info("")
            logger.info("⚠️ FILTERS BLOCKING REAL TRADES:")
            for f in worst:
                logger.info(
                    f"  {f['filter']}: {f['false_positive_rate']:.0%} "
                    f"({f['real_missed']}/{f['total_blocks']} real missed)"
                )
        logger.info("=" * 60)
