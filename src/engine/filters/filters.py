"""
Trading Filters for BTC Perpetual System
Implements: Kill Conditions, HTF Filter, MTF Structure, LTF Trigger
"""

import numpy as np
import pandas as pd
from datetime import datetime, timedelta, UTC
from typing import Optional, Tuple
import logging

from src.engine.config import (
    KillConditions, HTFFilters, MTFFilters,
    get_session_name
)
from src.engine.state.state_machine import BotState
from src.engine.indicators import (
    calculate_ema, calculate_adx, calculate_atr,
    calculate_relative_volume, calculate_orderbook_imbalance,
    check_hl_lh_structure, calculate_fibonacci_levels
)

logger = logging.getLogger(__name__)


class ImpulseTracker:
    """Tracks valid impulse legs for trend entries"""
    
    def __init__(self):
        self.impulse_high = None
        self.impulse_low = None
        self.impulse_start_time = None
        self.bos_confirmed = False
        self.impulse_atr = None  # ATR at impulse time
    
    def update_impulse(self, current_high: float, current_low: float, 
                      current_atr: float, bos_confirmed: bool, market_time: datetime | None = None):
        """Update impulse after BOS confirmation"""
        if bos_confirmed and not self.bos_confirmed:
            # New impulse starts
            self.impulse_high = current_high
            self.impulse_low = current_low
            # 🔒 FIX: Use market_time to prevent lookahead bias
            self.impulse_start_time = market_time if market_time else datetime.now(UTC)
            self.impulse_atr = current_atr
            self.bos_confirmed = True
            logger.info(f"IMPULSE_START: High={current_high:.2f}, Low={current_low:.2f}, ATR={current_atr:.2f}")
    
    def get_pullback_depth(self, pullback_low: float, atr_5m: float) -> float:
        """Get normalized pullback depth from impulse"""
        if not self.impulse_high:
            return 0
        
        pullback_depth = self.impulse_high - pullback_low
        normalized = pullback_depth / atr_5m if atr_5m > 0 else 0
        return normalized
    
    def log_impulse_state(self, current_price: float, pullback_low: float, atr_5m: float):
        """Log detailed impulse information"""
        if self.impulse_high and self.impulse_low:
            pullback = self.get_pullback_depth(pullback_low, atr_5m)
            impulse_range = self.impulse_high - self.impulse_low
            logger.debug(f"IMPULSE_STATE: High={self.impulse_high:.2f} Low={self.impulse_low:.2f} "
                        f"Range={impulse_range:.2f} | PullbackLow={pullback_low:.2f} "
                        f"NormalizedPullback={pullback:.2f}ATR | CurrentPrice={current_price:.2f}")


class KillConditionsFilter:
    """BLOQUE 0 - Kill Conditions"""
    
    def __init__(self, state_machine, capital: float = 10000.0, websocket_manager=None, risk_engine=None, backtest_mode: bool = False):
        self.state_machine = state_machine
        self._capital = capital  # Initial/fallback capital
        self._risk_engine = risk_engine  # Reference to get live capital
        self.websocket_manager = websocket_manager
        self.last_heartbeat: Optional[datetime] = None  # Will be set with market_time
        self.funding_rate = 0.0
        self.macro_event_detected = False
        self.backtest_mode = backtest_mode  # SKIP drawdown/loss observation in backtest
    
    @property
    def capital(self) -> float:
        """Get capital from risk_engine if available, else use stored value"""
        if self._risk_engine is not None and hasattr(self._risk_engine, 'capital') and self._risk_engine.capital > 0:
            return self._risk_engine.capital
        return self._capital if self._capital > 0 else 10000.0  # Fallback to 10k
        
    def check(self, current_price: float, atr_1m: float, market_time: datetime) -> Tuple[bool, str]:
        """
        Check all kill conditions
        
        Args:
            current_price: Current market price
            atr_1m: Current ATR on 1m
            market_time: Current market time (REQUIRED - never use datetime.now())
        
        Returns: (should_kill: bool, reason: str)
        
        MEJORADO (FIX #8):
        - 3 pérdidas consecutivas → OBSERVATION MODE (no apagar)
        - Drawdown diario → OBSERVATION MODE (no apagar)
        - Solo causas CRÍTICAS → APAGAR TODO (WebSocket, funding extremo, etc)
        """
        
        # 1. CRÍTICA: Daily drawdown >= 3% → OBSERVATION pero NO apagar
        # SKIP in backtest - drawdown accumulates unrealistically without daily resets
        if not self.backtest_mode:
            # 🔥 FIX: Use daily_pnl (absolute) not the old daily_drawdown attribute
            daily_pnl = getattr(self.state_machine, 'daily_pnl', 0.0)
            capital = self.capital if self.capital > 0 else 1.0
            dd_pct = abs(min(0, daily_pnl) / capital) * 100  # Only count losses
            if dd_pct >= KillConditions.MAX_DAILY_DRAWDOWN_PCT:
                logger.warning(f"⚠️ Daily drawdown {dd_pct:.2f}% >= {KillConditions.MAX_DAILY_DRAWDOWN_PCT}% - entering observation")
                self.state_machine.enter_observation_mode(f"Daily drawdown {dd_pct:.2f}%")
                return False, ""  # No apagar, solo observation
        
        # 2. CRÍTICA: Consecutive losses >= 3 → OBSERVATION pero NO apagar
        # SKIP in backtest - prevents testing losing strategies
        if not self.backtest_mode:
            if self.state_machine.consecutive_losses >= KillConditions.MAX_CONSECUTIVE_LOSSES:
                # Only log once when first entering observation (not on every tick)
                if self.state_machine.state != BotState.OBSERVATION:
                    logger.warning(f"⚠️ Consecutive losses {self.state_machine.consecutive_losses} >= {KillConditions.MAX_CONSECUTIVE_LOSSES} - entering observation")
                self.state_machine.enter_observation_mode(f"Consecutive losses: {self.state_machine.consecutive_losses}")
                return False, ""  # No apagar, solo observation
        
        # 3. EXTREMA: Funding rate check → APAGAR (riesgo sistémico)
        if abs(self.funding_rate) >= KillConditions.MAX_FUNDING_RATE:
            return True, f"Funding rate {self.funding_rate:.4f} exceeds threshold {KillConditions.MAX_FUNDING_RATE}"
        
        # 4. EXTREMA: Macro event detected → APAGAR
        if self.macro_event_detected:
            return True, "Macro event detected"
        
        # 5. EXTREMA: WebSocket silence check → APAGAR (pérdida de conexión)
        if self.websocket_manager and self.last_heartbeat:
            silence_seconds = (market_time - self.last_heartbeat).total_seconds()
            if silence_seconds > KillConditions.WEBSOCKET_TIMEOUT_SECONDS:
                logger.critical(f"WebSocket silent for {silence_seconds}s - EMERGENCY EXIT")
                # Should trigger emergency position close via REST
                return True, f"WebSocket timeout ({silence_seconds}s)"
        
        return False, ""
    
    def update_heartbeat(self, market_time: datetime):
        """Update last heartbeat timestamp using market time"""
        self.last_heartbeat = market_time
    
    def set_funding_rate(self, rate: float):
        """Update funding rate"""
        self.funding_rate = rate
    
    def set_macro_event(self, detected: bool):
        """Set macro event flag"""
        self.macro_event_detected = detected


class BlackSwanFilter:
    """
    Filter for extreme price movements (Cisne Negro)
    
    IMPROVED VERSION:
    - Defensive mode at 3x ATR (reduce size, don't block)
    - Full cooldown only with spike + reversal (4x ATR + high wick)
    - Cooldown doesn't restart if already active
    - Uses ATR from higher timeframe when available
    - ALL time logic uses market_time parameter (same code for live and backtest)
    """
    
    def __init__(self):
        self.cooldown_until: Optional[datetime] = None
        self.defensive_mode: bool = False
        self.defensive_until: Optional[datetime] = None
    
    def check(self, price_change: float, atr: float, market_time: datetime,
              candle_high: float | None = None, candle_low: float | None = None,
              candle_open: float | None = None, candle_close: float | None = None) -> Tuple[bool, str]:
        """
        Check for black swan conditions
        
        Args:
            price_change: Absolute price change of candle
            atr: ATR value (preferably 5m for stability)
            market_time: Current market time (REQUIRED - never use datetime.now())
            candle_high/low/open/close: For wick ratio calculation
        
        Returns: (is_black_swan: bool, reason: str)
        """
        # 1) If already in cooldown, DON'T restart - just return status
        if self.cooldown_until and market_time < self.cooldown_until:
            remaining = (self.cooldown_until - market_time).total_seconds()
            return True, f"Cooldown active ({remaining:.0f}s remaining)"
        
        # Clear expired cooldown
        if self.cooldown_until and market_time >= self.cooldown_until:
            self.cooldown_until = None
            logger.info("Black swan cooldown expired - back to normal")
        
        # 2) Check defensive mode expiry
        if self.defensive_until and market_time >= self.defensive_until:
            self.defensive_mode = False
            self.defensive_until = None
            logger.info("Defensive mode expired - back to normal")
        
        abs_change = abs(price_change)
        
        # 3) Level 2: TRUE Black Swan - spike + violent reversal (wick)
        #    Only activate FULL COOLDOWN if:
        #    - Move >= 4x ATR AND
        #    - Wick ratio > 60% (sign of liquidations/chaos)
        black_swan_threshold = KillConditions.BLACK_SWAN_MULTIPLIER * atr
        
        if abs_change >= black_swan_threshold and candle_high and candle_low and candle_open and candle_close:
            wick_ratio = self._calculate_wick_ratio(candle_high, candle_low, candle_open, candle_close)
            
            if wick_ratio >= KillConditions.BLACK_SWAN_WICK_RATIO:
                self.activate_cooldown(market_time)
                return True, f"BLACK SWAN: {abs_change:.0f} >= {black_swan_threshold:.0f} (4x ATR) + wick {wick_ratio:.0%}"
        
        # 4) Level 1: Defensive mode - large move but not catastrophic
        #    Reduce position size, tighter stops, but DON'T stop trading
        defensive_threshold = KillConditions.DEFENSIVE_MODE_MULTIPLIER * atr
        
        if abs_change >= defensive_threshold:
            self._activate_defensive_mode(market_time)
            # Return False - not a full block, just defensive
            return False, f"DEFENSIVE MODE: {abs_change:.0f} >= {defensive_threshold:.0f} (3x ATR)"
        
        return False, ""
    
    def _calculate_wick_ratio(self, high: float, low: float, open_: float, close: float) -> float:
        """
        Calculate wick ratio (how much of the candle is wick vs body)
        High wick ratio = potential liquidation cascade / reversal
        """
        candle_range = high - low
        if candle_range == 0:
            return 0.0
        
        body = abs(close - open_)
        wick = candle_range - body
        
        return wick / candle_range
    
    def _activate_defensive_mode(self, market_time: datetime, duration_minutes: int = 5):
        """Activate defensive mode (reduced size, not full stop)"""
        if not self.defensive_mode:
            self.defensive_mode = True
            self.defensive_until = market_time + timedelta(minutes=duration_minutes)
            logger.warning(f"⚠️ DEFENSIVE MODE activated for {duration_minutes} min")
    
    def activate_cooldown(self, market_time: datetime):
        """Activate full black swan cooldown"""
        self.cooldown_until = market_time + timedelta(minutes=KillConditions.BLACK_SWAN_COOLDOWN_MINUTES)
        self.defensive_mode = False  # Cooldown supersedes defensive
        logger.critical(f"🦢 BLACK SWAN - Full cooldown until {self.cooldown_until.strftime('%H:%M:%S')}")
    
    def is_defensive(self, market_time: datetime) -> bool:
        """Check if in defensive mode (reduce size but keep trading)"""
        # Check if defensive mode is active AND not expired
        if self.defensive_mode and self.defensive_until and market_time < self.defensive_until:
            return True
        # Clear expired state
        if self.defensive_mode and self.defensive_until and market_time >= self.defensive_until:
            self.defensive_mode = False
            self.defensive_until = None
        return False
    
    def get_size_multiplier(self, market_time: datetime) -> float:
        """
        Get position size multiplier based on current mode
        - Normal: 1.0
        - Defensive: 0.5 (50% size)
        - Cooldown: 0.0 (no trading)
        
        Args:
            market_time: Current market time (REQUIRED)
        """
        if self.cooldown_until and market_time < self.cooldown_until:
            return 0.0
        if self.is_defensive(market_time):
            return 0.5
        return 1.0


class HTFFilter:
    """BLOQUE 1 - High Timeframe Filter (1H)
    
    BACKTEST MODE FIX (Jan 4, 2026):
    - Simplified to work with limited data
    - Uses EMA50 only (not EMA200) when data < 200 candles
    - Logs all decisions for debugging
    
    FIX (Jan 23, 2026): ADX Classification
    - ADX < 18: RANGE mode (scalp stops, mean-reversion)
    - ADX 18-25: WEAK_TREND mode (cautious trend-following)
    - ADX >= 25: TREND mode (full trend-following)
    """
    
    def __init__(self, backtest_mode: bool = False):
        self.backtest_mode = backtest_mode
        # 🔥 FIX (Jan 23, 2026): Store last regime mode for downstream use
        self.last_htf_regime_mode: str = 'UNKNOWN'
        self.last_htf_adx: float = 0.0
    
    def check(self, df_1h: pd.DataFrame, current_price: float, 
              correlation_data: Optional[dict] = None) -> Tuple[bool, Optional[str], str]:
        """
        Check HTF filters
        Returns: (passed: bool, bias: Optional[str], reason: str)
        bias can be 'LONG' or 'SHORT'
        
        After calling check(), access:
        - self.last_htf_regime_mode: 'RANGE', 'WEAK_TREND', 'TREND', or 'LOW_ADX'
        - self.last_htf_adx: The ADX value used for classification
        """
        # Minimum data requirement: need at least 50 candles for EMA50
        if len(df_1h) < 50:
            logger.warning(f"HTF: Insufficient data ({len(df_1h)} < 50 candles)")
            return False, None, f"Insufficient data for HTF filter ({len(df_1h)} < 50 candles)"
        
        # Calculate indicators
        closes = df_1h['close'].to_numpy()
        highs = df_1h['high'].to_numpy()
        lows = df_1h['low'].to_numpy()
        
        ema_50 = calculate_ema(closes, 50)[-1]
        
        # EMA200: only use if we have enough data, otherwise use EMA50 as reference
        if len(df_1h) >= 200:
            ema_200 = calculate_ema(closes, 200)[-1]
        else:
            # BACKTEST FIX: Use EMA50 as the only trend reference
            ema_200 = ema_50  # This makes EMA50 > EMA200 always neutral
            logger.debug(f"HTF: Using simplified mode (only {len(df_1h)} candles, EMA200 set to EMA50)")
        
        # Check for NaN in EMAs
        if np.isnan(ema_50):
            logger.warning(f"HTF: EMA50 is NaN - cannot determine bias")
            return False, None, "EMA50 calculation failed (NaN)"
        
        adx_values, adx_slope = calculate_adx(highs, lows, closes, 14)
        # 🔥 FIX (Jan 25, 2026): Ensure ADX is a Python float, not numpy array
        adx = float(adx_values[-1]) if len(adx_values) > 0 else 0.0
        slope = float(adx_slope[-1]) if len(adx_slope) > 0 else 0.0
        
        # FIX: Handle NaN ADX - use default values instead of blocking
        if np.isnan(adx):
            logger.warning(f"HTF: ADX is NaN - using default value 20")
            adx = 20.0  # Assume moderate trend instead of blocking
        if np.isnan(slope):
            slope = 0.0
        
        atr_values = calculate_atr(highs, lows, closes, 14)
        atr = atr_values[-1] if len(atr_values) > 0 else 0.0
        atr_ma = np.mean(atr_values[-20:]) if len(atr_values) >= 20 else atr
        
        # Check for NaN ATR
        if np.isnan(atr) or atr == 0:
            logger.warning(f"HTF: ATR is NaN or 0 - using price-based estimate")
            atr = current_price * 0.001  # 0.1% of price as fallback
            atr_ma = atr
        
        # =========================================================
        # SIMPLIFIED BIAS DETERMINATION WITH HYSTERESIS
        # =========================================================
        # Primary: Price vs EMA50 (most reliable with limited data)
        # 
        # HYSTERESIS: To avoid rapid bias flips when price is near EMA50,
        # we require a minimum buffer (0.05% of price, or ~50 points on BTC)
        # Price must be clearly above/below EMA50 to change bias
        # =========================================================
        bias = None
        
        # Calculate hysteresis buffer (0.05% of price, minimum 30 points for BTC)
        hysteresis_pct = 0.0005  # 0.05%
        hysteresis_buffer = max(current_price * hysteresis_pct, 30.0)
        
        if current_price > ema_50 + hysteresis_buffer:
            bias = 'LONG'
        elif current_price < ema_50 - hysteresis_buffer:
            bias = 'SHORT'
        else:
            # Price within hysteresis zone - use trend direction from recent candles
            if len(closes) >= 5:
                # Use 5-candle momentum direction
                momentum = closes[-1] - closes[-5]
                if momentum > 0:
                    bias = 'LONG'
                elif momentum < 0:
                    bias = 'SHORT'
                else:
                    # Truly flat - default to price relative to EMA50
                    bias = 'LONG' if current_price >= ema_50 else 'SHORT'
            else:
                bias = 'LONG' if current_price >= ema_50 else 'SHORT'
            
            logger.debug(f"📊 HTF: Price in hysteresis zone (±{hysteresis_buffer:.0f} around EMA50={ema_50:.2f})")
        
        # Log bias decision (DEBUG level to reduce noise)
        logger.debug(f"[DATA] HTF bias: Price={current_price:.2f} | EMA50={ema_50:.2f} | EMA200={ema_200:.2f} | ADX={adx:.2f} | Slope={slope:.4f} -> bias={bias}")
        
        # =========================================================
        # RELAXED FILTERS FOR BACKTEST MODE
        # =========================================================
        if self.backtest_mode:
            # In backtest: only require a valid bias, skip other checks
            if bias is not None:
                logger.info(f"✅ HTF Filter PASSED (backtest mode) - Bias: {bias}")
                return True, bias, "HTF filter passed (backtest mode)"
            else:
                return False, None, "No bias determined"
        
        # =========================================================
        # FULL VALIDATION FOR LIVE TRADING
        # =========================================================
        
        # 🔥 FIX (Jan 23, 2026): ADX is a CLASSIFIER with CORRECT thresholds
        # ADX < 18: RANGE → scalp logic, 2.0x ATR stops, mean-reversion
        # ADX 18-25: WEAK_TREND → cautious trend-following
        # ADX >= 25: TREND → full trend logic, 3.0x ATR stops
        #
        # Previous bug: ADX 15.3 was classified as TREND → wrong!
        
        # Determine regime based on ADX with CORRECT thresholds
        if adx >= HTFFilters.ADX_TREND_MIN:
            regime_mode = 'TREND'
        elif adx >= HTFFilters.ADX_RANGE_MAX:
            regime_mode = 'WEAK_TREND'
        elif adx >= 12:
            regime_mode = 'RANGE'
        else:
            regime_mode = 'LOW_ADX'  # Very low ADX < 12, chop
        
        # 🔥 Store for downstream access
        self.last_htf_regime_mode = regime_mode
        self.last_htf_adx = adx
        
        logger.debug(f"[DATA] HTF ADX={adx:.1f} -> Mode: {regime_mode}")
        
        # ADX NO LONGER BLOCKS - but mode is critical for downstream logic
        if adx < HTFFilters.ADX_RANGE_MAX:
            logger.debug(f"[DATA] HTF: ADX {adx:.2f} < {HTFFilters.ADX_RANGE_MAX} -> {regime_mode} mode")
        
        # =========================================================
        # ADX SLOPE - INFORMATION ONLY, NEVER BLOCKS
        # 
        # ADX slope negativo ≠ tendencia colapsando
        # ADX mide fuerza PASADA, no reversión
        # Cae durante pullbacks normales
        # 
        # Colapso real = estructura (price < EMA50), no indicador
        # =========================================================
        # slope is logged and returned for tagging, but NEVER blocks entry
        if slope < -0.5:
            logger.debug(f"[DATA] HTF: ADX slope negative ({slope:.3f})")
        
        # Check ATR
        if atr <= atr_ma * HTFFilters.MIN_ATR_VS_MA:
            return False, None, f"ATR {atr:.2f} <= MA {atr_ma:.2f}"
        
        # Check correlation (NQ/ES)
        if HTFFilters.CHECK_CORRELATION and correlation_data:
            if not self._check_correlation(bias, correlation_data):
                return False, None, f"Correlation check failed for {bias}"
        
        logger.debug(f"[OK] HTF Filter PASSED - Bias: {bias} | ADX: {adx:.2f}")
        return True, bias, "HTF filter passed"
    
    def _check_correlation(self, bias: str, correlation_data: dict) -> bool:
        """Check macro correlation with NQ/ES"""
        # For LONG: NQ and ES should not be making Lower Lows
        # For SHORT: NQ and ES should not be breaking highs
        
        # Simplified check - in production, would analyze actual price structure
        if bias == 'LONG':
            # Check if indices are not making lower lows
            for symbol in ['NQ', 'ES']:
                if symbol in correlation_data:
                    data = correlation_data[symbol]
                    if self._is_making_lower_lows(data):
                        logger.warning(f"{symbol} making lower lows - blocking LONG")
                        return False
        
        elif bias == 'SHORT':
            # Check if indices are not breaking highs
            for symbol in ['NQ', 'ES']:
                if symbol in correlation_data:
                    data = correlation_data[symbol]
                    if self._is_breaking_highs(data):
                        logger.warning(f"{symbol} breaking highs - blocking SHORT")
                        return False
        
        return True
    
    def _is_making_lower_lows(self, df: pd.DataFrame, periods: int = 3) -> bool:
        """Check if making lower lows"""
        if len(df) < periods + 1:
            return False
        
        recent_lows = df['low'].to_numpy()[-periods:]
        for i in range(1, len(recent_lows)):
            if recent_lows[i] < recent_lows[i-1]:
                return True
        return False
    
    def _is_breaking_highs(self, df: pd.DataFrame, periods: int = 3) -> bool:
        """Check if breaking recent highs"""
        if len(df) < periods + 1:
            return False
        
        recent_highs = df['high'].to_numpy()[-periods-1:-1]
        current_high = df['high'].iloc[-1]
        
        return current_high > float(np.amax(np.asarray(recent_highs)))


class MTFStructureFilter:
    """BLOQUE 2 - Mid Timeframe Structure Filter (5M)
    
    FILOSOFÍA CORRECTA:
    - En tendencias fuertes (ADX > 30): Continuación > geometría perfecta
    - En tendencias débiles: Exigir estructura limpia
    - HL/LH solo para detección negativa (rompimiento)
    """
    
    def check(self, df_5m: pd.DataFrame, bias: str, impulse_high: float, 
              impulse_low: float, htf_adx: float = 0) -> Tuple[bool, str]:
        """Check MTF structure filters based on Regime"""
        if len(df_5m) < 50:
            return False, "Insufficient data for MTF filter"
        
        current_price = df_5m['close'].iloc[-1]
        highs = df_5m['high'].to_numpy()
        lows = df_5m['low'].to_numpy()
        closes = df_5m['close'].to_numpy()
        
        # Calculate indicators
        ema_20 = calculate_ema(closes, 20)[-1]
        ema_50 = calculate_ema(closes, 50)[-1]
        atr_5m = calculate_atr(highs, lows, closes, 14)[-1]
        
        logger.debug(f"MTF: Price={current_price:.2f}, EMA20={ema_20:.2f}, ADX={htf_adx:.1f}")
        
        # 1. CLASIFICACIÓN DEL RÉGIMEN
        regime = 'STRUCTURE'
        if htf_adx >= MTFFilters.ADX_TREND_STRONG:
            regime = 'TREND'
        
        logger.debug(f"MTF REGIME: {regime} (ADX={htf_adx:.1f})")
        
        if regime == 'TREND':
            return self._check_trend_regime(current_price, ema_20, ema_50, 
                                          highs, lows, closes, atr_5m, bias)
        else:
            return self._check_structure_regime(current_price, ema_20, ema_50,
                                              highs, lows, closes, atr_5m, bias)
    
    def _check_trend_regime(self, current_price: float, ema_20: float, ema_50: float,
                           highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                           atr_5m: float, bias: str) -> Tuple[bool, str]:
        """
        MTF EN RÉGIMEN TREND (ADX FUERTE >= 30)
        
        CORRECCIÓN CRÍTICA:
        - Pullback se calcula como: (impulse_high - pullback_low) / atr_5m
        - NO como: (last_high - current_price)
        - Thresholds ajustados a BTC real: 0.8-1.2 ATR
        """
        
        # 1. Validar INVALIDACIONES (Filtros Negativos)
        if bias == 'LONG' and current_price < ema_50:
            return False, f"Trend invalidated: Price {current_price:.2f} < EMA50 {ema_50:.2f}"
        if bias == 'SHORT' and current_price > ema_50:
            return False, f"Trend invalidated: Price {current_price:.2f} > EMA50 {ema_50:.2f}"
            
        # 2. Pullback CORRECTO: desde impulso, no desde último high
        if bias == 'LONG':
            # Impulse high = máximo de últimos 10 candles (impulso real)
            impulse_high = np.max(highs[-10:])
            # Pullback low = mínimo en pullback (últimos 5 candles)
            pullback_low = np.min(lows[-5:])
            
            # Profundidad normalizada
            pullback_depth = impulse_high - pullback_low
            normalized_pullback = pullback_depth / atr_5m if atr_5m > 0 else 0
            
            # BTC con ADX fuerte permite más pullback (1.3 ATR para capturar setups típicos)
            max_allowed_pullback = 1.3  # ATR units - allows realistic BTC structure
            
            logger.debug(f"TREND_LONG: ImpulseHigh={impulse_high:.2f} PullbackLow={pullback_low:.2f} "
                       f"Depth={pullback_depth:.2f} (${pullback_depth/atr_5m:.2f}ATR) MaxAllowed={max_allowed_pullback}ATR")
            
            if normalized_pullback > max_allowed_pullback:
                # Escape: Si pullback es muy profundo pero precio > EMA50, pasar a wait
                if current_price > ema_50:
                    logger.debug(f"Pullback deep ({normalized_pullback:.2f}ATR) but price > EMA50 - Waiting for LTF")
                    return True, "Deep pullback - waiting for LTF trigger (price > EMA50)"
                else:
                    return False, f"Pullback too deep: {normalized_pullback:.2f}ATR > {max_allowed_pullback}ATR"
            
            # 3. Reclaim de EMA20 (relaxed tolerance for entry opportunities)
            above_ema20 = current_price >= (ema_20 * 0.95)  # 5% tolerance (was 1%)
            if not above_ema20:
                return False, f"Price {current_price:.2f} below EMA20 floor {ema_20*0.95:.2f}"
                
            logger.info(f"✅ TREND_LONG CONFIRMED: Pullback={normalized_pullback:.2f}ATR + EMA20 reclaim")
            return True, f"Trend LONG: {normalized_pullback:.2f}ATR pullback + EMA20 reclaim"
            
        else:  # SHORT
            impulse_low = np.min(lows[-10:])
            pullback_high = np.max(highs[-5:])
            
            pullback_depth = pullback_high - impulse_low
            normalized_pullback = pullback_depth / atr_5m if atr_5m > 0 else 0
            
            max_allowed_pullback = 1.3  # ATR units
            
            logger.debug(f"TREND_SHORT: ImpulseLow={impulse_low:.2f} PullbackHigh={pullback_high:.2f} "
                       f"Depth={pullback_depth:.2f} ({normalized_pullback:.2f}ATR) MaxAllowed={max_allowed_pullback}ATR")
            
            if normalized_pullback > max_allowed_pullback:
                if current_price < ema_50:
                    logger.debug(f"Pullback deep ({normalized_pullback:.2f}ATR) but price < EMA50 - Waiting for LTF")
                    return True, "Deep pullback - waiting for LTF trigger (price < EMA50)"
                else:
                    return False, f"Pullback too deep: {normalized_pullback:.2f}ATR > {max_allowed_pullback}ATR"
            
            # Reclaim de EMA20 (relaxed tolerance for entry opportunities)
            below_ema20 = current_price <= (ema_20 * 1.05)  # 5% tolerance (was 1%)
            if not below_ema20:
                logger.warning(f"SHORT rejected: Price {current_price:.2f} above EMA20 ceiling {ema_20*1.05:.2f}")
                return False, f"Price {current_price:.2f} above EMA20 ceiling {ema_20*1.05:.2f}"
                
            logger.info(f"✅ TREND_SHORT CONFIRMED: Pullback={normalized_pullback:.2f}ATR + EMA20 below")
            return True, f"Trend SHORT: {normalized_pullback:.2f}ATR pullback + below EMA20"

    def _check_structure_regime(self, current_price: float, ema_20: float, ema_50: float,
                               highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                               atr_5m: float, bias: str) -> Tuple[bool, str]:
        """
        MTF EN RÉGIMEN STRUCTURE (ADX < 30)
        Exige geometría estricta.
        """
        # 1. Estructura HL/LH válida
        structure_valid = check_hl_lh_structure(highs, lows, bias.lower(), MTFFilters.HL_LH_PERIODS)
        if not structure_valid:
            return False, "Structure Regime: No valid HL/LH sequence"
        
        # 2. Fib Retracement
        # Simplificado: el precio debe estar en zona razonable, no sobre-extendido
        # En código completo usaría calculate_fibonacci_levels
        
        # 3. BOS Estricto
        if not self._check_bos(highs, lows, closes, bias):
             return False, "Structure Regime: No strict BOS"
             
        return True, "Structure Regime: Valid Geometry"
    
    def _check_bos(self, highs: np.ndarray, lows: np.ndarray, 
                   closes: np.ndarray, bias: str) -> bool:
        """Check for Break of Structure (strict)"""
        lookback = MTFFilters.BOS_LOOKBACK_PERIODS
        
        if bias == 'LONG':
            recent_high = np.max(highs[-lookback-1:-1])
            return closes[-1] > recent_high
        else:  # SHORT
            recent_low = np.min(lows[-lookback-1:-1])
            return closes[-1] < recent_low
    
    def _check_bos_relaxed(self, highs: np.ndarray, lows: np.ndarray,
                          closes: np.ndarray, bias: str) -> bool:
        """Check for valid structure - BOS STRICT or WEAK VALID SHIFT"""
        lookback = MTFFilters.BOS_LOOKBACK_PERIODS
        
        if bias == 'LONG':
            # Strict BOS: close above recent high
            recent_high = np.max(highs[-lookback-1:-1])
            strict_bos = closes[-1] > recent_high
            
            if strict_bos:
                logger.debug(f"BOS_STRICT: Price {closes[-1]:.2f} > Recent High {recent_high:.2f}")
                return True
            
            # WEAK BOS: Valid shift (lower low + higher high in last 5 candles)
            if len(closes) >= 5:
                recent_ll = np.min(lows[-8:-1])  # Last 8 candles (excluding current)
                current_candle_close = closes[-1]
                
                # Reclaim of level + close near/above EMA
                ema_20 = calculate_ema(closes, 20)[-1]
                at_or_above_ema = current_candle_close >= (ema_20 * 0.995)  # 0.5% tolerance
                
                # Pullback + recovery pattern
                recent_range = np.max(highs[-5:]) - np.min(lows[-5:])
                is_in_range = recent_ll <= current_candle_close <= (np.max(highs[-5:]) * 1.01)
                
                weak_bos_valid = at_or_above_ema and is_in_range
                
                if weak_bos_valid and KillConditions.ALLOW_WEAK_BOS:
                    logger.info(f"BOS_WEAK_ACCEPTED: Price {current_candle_close:.2f} near EMA20 {ema_20:.2f}")
                    return True
                elif weak_bos_valid:
                    logger.debug(f"BOS_WEAK_VALID: Price {current_candle_close:.2f} near EMA20 {ema_20:.2f} (disabled)")
                    return False
            
            return False
        
        else:  # SHORT
            # Strict BOS: close below recent low
            recent_low = np.min(lows[-lookback-1:-1])
            strict_bos = closes[-1] < recent_low
            
            if strict_bos:
                logger.debug(f"BOS_STRICT: Price {closes[-1]:.2f} < Recent Low {recent_low:.2f}")
                return True
            
            # WEAK BOS: Valid shift (higher high + lower low in last 5 candles)
            if len(closes) >= 5:
                recent_hh = np.max(highs[-8:-1])
                current_candle_close = closes[-1]
                
                # Reclaim of level + close near/below EMA
                ema_20 = calculate_ema(closes, 20)[-1]
                at_or_below_ema = current_candle_close <= (ema_20 * 1.005)  # 0.5% tolerance
                
                # Pullback + recovery pattern
                recent_range = np.max(highs[-5:]) - np.min(lows[-5:])
                is_in_range = current_candle_close >= (np.min(lows[-5:]) * 0.99) and current_candle_close <= recent_hh
                
                weak_bos_valid = at_or_below_ema and is_in_range
                
                if weak_bos_valid and KillConditions.ALLOW_WEAK_BOS:
                    logger.info(f"BOS_WEAK_ACCEPTED: Price {current_candle_close:.2f} near EMA20 {ema_20:.2f}")
                    return True
                elif weak_bos_valid:
                    logger.debug(f"BOS_WEAK_VALID: Price {current_candle_close:.2f} near EMA20 {ema_20:.2f} (disabled)")
                    return False
            
            return False
    
    def _check_fibonacci_zone(self, price: float, fibo_levels: dict) -> bool:
        """Check if price is in fibonacci retracement zone"""
        fibo_382 = fibo_levels['38.2']
        fibo_618 = fibo_levels['61.8']
        
        return min(fibo_382, fibo_618) <= price <= max(fibo_382, fibo_618)
    
    def _count_correction_candles(self, df: pd.DataFrame, bias: str) -> int:
        """Count correction candles"""
        # Simplified - count recent opposite direction candles
        count = 0
        for i in range(len(df) - 1, max(0, len(df) - 5), -1):
            candle_direction = 'up' if df['close'].iloc[i] > df['open'].iloc[i] else 'down'
            
            if bias == 'LONG' and candle_direction == 'down':
                count += 1
            elif bias == 'SHORT' and candle_direction == 'up':
                count += 1
            else:
                break
        
        return count
    
    def _check_near_vwap(self, price: float, vwap: float, tolerance_pct: float = 0.5) -> bool:
        """Check if price is near VWAP"""
        distance_pct = abs(price - vwap) / vwap * 100
        return distance_pct <= tolerance_pct


class LTFTriggerFilter:
    """BLOQUE 3 - Low Timeframe Trigger Filter (1M)
    
    FILOSOFÍA CORRECTA:
    - Trigger = cambio de estructura de precio (BOS, EMA reclaim, slope)
    - Volumen = confirmación, BLOQUEA SOLO en régimen de expansión
    - Orderbook = información adicional, NUNCA filtro bloqueador
    
    LÓGICA DE VOLUMEN POR RÉGIMEN:
    - Expansion/Trend (REGIME_A): Volumen DEBE estar presente → bloquear si muy bajo
    - Compression (REGIME_B): Volumen bajo es esperado → NO bloquear
    - Chop (REGIME_D): Volumen bajo es normal → NO bloquear
    
    BACKTEST MODE:
    - Volume filters disabled (Kraken data may have V=0)
    """
    
    # Umbrales de volumen por régimen
    VOLUME_BLOCK_THRESHOLD_EXPANSION = 0.5  # En expansión, bloquear si < 50% del promedio
    # FIX (Jan 3, 2026): Reduced volume threshold from 0.25 to 0.05 for diagnosis
    # Kraken Futures has very low volume on weekends
    VOLUME_BLOCK_THRESHOLD_DEFAULT = 0.05   # Fallback muy bajo - solo trades muertos
    
    def __init__(self, backtest_mode: bool = False):
        self.backtest_mode = backtest_mode
        self._last_ltf_info = {}  # Store LTF details for trade tags
    
    def check(self, df_1m: pd.DataFrame, bias: str, orderbook: dict | None = None,
              regime: str | None = None, regime_confidence: float = 0.0) -> Tuple[bool, str]:
        """
        Check LTF trigger conditions (PRICE-BASED PRIMARY)
        
        Args:
            df_1m: DataFrame con velas de 1m
            bias: 'LONG' o 'SHORT'
            orderbook: Datos de order book (opcional)
            regime: Régimen actual del mercado (opcional, de EdgeRegimeClassifier)
            regime_confidence: Confianza del régimen (0-1)
        
        Returns: (passed: bool, reason: str)
        """
        if len(df_1m) < 20:
            return False, "Insufficient data for LTF filter"
        
        current_candle = df_1m.iloc[-1]
        closes = df_1m['close'].to_numpy()
        volumes = df_1m['volume'].to_numpy()
        highs = df_1m['high'].to_numpy()
        lows = df_1m['low'].to_numpy()
        
        current_price = closes[-1]
        
        # ===== PRIMARY TRIGGER: PRICE STRUCTURE =====
        
        # 1. BOS LTF (Break of Structure en 1m) - relaxed
        recent_high = float(np.amax(np.asarray(highs[-8:-1])))  # Last 8 candles (excluding current)
        recent_low = float(np.amin(np.asarray(lows[-8:-1])))
        
        bos_confirmed = False
        
        if bias == 'LONG':
            # Allow close OR high to break structure (more permissive)
            bos_confirmed = closes[-1] > recent_high or highs[-1] > recent_high
            logger.debug(f"LTF: CurrentPrice={current_price:.2f} vs RecentHigh={recent_high:.2f} (BOS: {bos_confirmed})")
        else:
            # Allow close OR low to break structure (more permissive)
            bos_confirmed = closes[-1] < recent_low or lows[-1] < recent_low
            logger.debug(f"LTF: CurrentPrice={current_price:.2f} vs RecentLow={recent_low:.2f} (BOS: {bos_confirmed})")
        
        # 2. EMA Reclaim - slightly more permissive
        ema_9 = calculate_ema(closes, 9)[-1]
        ema_20 = calculate_ema(closes, 20)[-1]
        
        ema_reclaim = False
        if bias == 'LONG':
            ema_reclaim = current_price >= (ema_9 * 0.995)  # 0.5% tolerance (was 1%)
        else:
            ema_reclaim = current_price <= (ema_9 * 1.005)
        
        logger.debug(f"LTF: EMA9={ema_9:.2f}, EMA20={ema_20:.2f}, Reclaim={ema_reclaim}")
        
        # 3. EMA Slope (momentum) - use 3-candle average for stability
        if len(closes) >= 5:
            ema_9_now = calculate_ema(closes, 9)[-1]
            ema_9_3ago = calculate_ema(closes[:-3], 9)[-1]
            ema_slope = (ema_9_now - ema_9_3ago) / 3  # Average per-candle slope
        else:
            ema_9_prev = calculate_ema(closes[:-1], 9)[-1]
            ema_slope = ema_9 - ema_9_prev
        
        # Slope threshold - need meaningful movement (at least 0.5 points per candle for BTC)
        if bias == 'LONG':
            slope_positive = ema_slope > -0.5  # Allow slightly negative (consolidation)
        else:
            slope_positive = ema_slope < 0.5   # Allow slightly positive (consolidation)
        logger.debug(f"LTF: EMASlope={ema_slope:.4f}, Positive={slope_positive}")
        
        # ===== DECISION LOGIC =====
        
        # SCORE > GATES: LTF never blocks, only contributes to score
        # Each confirmation adds to the LTF score (for use in entry threshold)
        confirmations = sum([bos_confirmed, ema_reclaim, slope_positive])
        
        # Store LTF details in filter_info for tags (accessible via last_ltf_info)
        self._last_ltf_info = {
            'bos_confirmed': bos_confirmed,
            'ema_reclaim': ema_reclaim,
            'slope_positive': slope_positive,
            'confirmations': confirmations,
            'ema_9': ema_9,
            'ema_20': ema_20,
            'ema_slope': ema_slope,
            'current_price': current_price
        }
        
        # INFO ONLY - Log confirmations at DEBUG level unless incomplete
        if confirmations < 3:
            logger.info(f"📊 LTF confirmations: {confirmations}/3 | BOS={bos_confirmed} | EMAReclaim={ema_reclaim} | Slope={slope_positive}")
        else:
            logger.debug(f"LTF confirmations: {confirmations}/3 | BOS={bos_confirmed} | EMAReclaim={ema_reclaim} | Slope={slope_positive}")
        
        # ===== SECONDARY: VOLUME (INFO ONLY - NEVER BLOCKS) =====
        # SCORE > GATES: Volume is tag + score contributor, NOT a gate
        # Low volume on pullbacks is NORMAL and often precedes expansion
        rel_vol = 1.0  # Default for backtest
        
        if self.backtest_mode:
            logger.debug(f"[BACKTEST] Skipping volume calculation")
        else:
            rel_vol = calculate_relative_volume(volumes, 20)[-1]
        
        # Classify volume for tags (never block)
        if rel_vol >= 1.0:
            volume_bucket = 'high'
        elif rel_vol >= 0.5:
            volume_bucket = 'normal'
        elif rel_vol >= 0.25:
            volume_bucket = 'low'
        else:
            volume_bucket = 'very_low'
        
        # Store volume info in LTF details for tags
        self._last_ltf_info['rel_vol'] = rel_vol
        self._last_ltf_info['volume_bucket'] = volume_bucket
        
        logger.debug(f"LTF volume: {rel_vol:.2f} ({volume_bucket})")
        logger.debug(f"LTF: {bias} | BOS={bos_confirmed} EMA={ema_reclaim} Slope={slope_positive} | Vol={volume_bucket} ({rel_vol:.2f})")
        
        # ===== TERTIARY: ORDERBOOK (INFORMATION ONLY) =====
        
        if orderbook and ('bids' in orderbook or 'asks' in orderbook):
            bids = orderbook.get('bids', [])
            asks = orderbook.get('asks', [])
            
            if bids and asks:
                imbalance = calculate_orderbook_imbalance(bids, asks, 10)  # Top 10 levels
                
                if bias == 'LONG' and imbalance > 50:
                    logger.info(f"  → Orderbook: BULLISH imbalance {imbalance:.1f}%")
                elif bias == 'SHORT' and imbalance < 50:
                    logger.info(f"  → Orderbook: BEARISH imbalance {100-imbalance:.1f}%")
                else:
                    logger.info(f"  → Orderbook: NEUTRAL {imbalance:.1f}%")
        
        return True, f"LTF trigger on price structure (BOS={bos_confirmed})"
    
    def get_last_ltf_info(self) -> dict:
        """Return LTF confirmation details for trade tags"""
        return self._last_ltf_info.copy() if self._last_ltf_info else {}
