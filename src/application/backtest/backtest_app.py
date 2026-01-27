"""
Professional Backtester - Integrates REAL Strategy Logic
Uses the same filters and logic as production signal_engine.py

UPDATED (Jan 4, 2026):
- Uses centralized PositionSizer from core_utils
- Integrates EdgeEnforcement for consistent validation
- Uses TimeProvider for consistent timestamps
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta, UTC
from typing import Dict, List, Tuple, Optional
import logging

from src.application.services.market_data import SynchronizedMultiTimeframeBuffer
from src.domain.market.filters import (
    KillConditionsFilter, BlackSwanFilter, HTFFilter, LTFTriggerFilter
)
from src.domain.risk.risk_engine import RiskEngine
from src.domain.trading.state.state_machine import StateMachine, BotState
from src.shared.indicators import (
    calculate_ema, calculate_atr, calculate_adx,
    calculate_relative_volume, calculate_orderbook_imbalance
)
from src.shared.utils import (
    safe_float, ensure_utc, validate_candle,
    PositionSizer, get_position_sizer
)
from src.shared.time_provider import get_time_provider, TimeProvider, reset_time_provider
from src.domain.trading.edge.edge_enforcement import EdgeEnforcement
from src.domain.market.regime.activity_regime import EnforcementMode

logger = logging.getLogger(__name__)


class BacktestTrade:
    """Representa un trade en el backtest"""
    def __init__(self, entry_time: datetime, entry_price: float, 
                 direction: str, size: float, stop_loss: float, entry_params: Dict | None = None):
        self.entry_time = entry_time
        self.entry_price = entry_price
        self.direction = direction
        self.size = size
        self.stop_loss = stop_loss
        self.entry_params = entry_params or {}
        
        self.exit_time: Optional[datetime] = None
        self.exit_price: float = 0.0
        self.exit_reason: str = ""
        self.htf_bias: str = ""
        self.mtf_signal: str = ""
        self.ltf_signal: str = ""
    
    @property
    def pnl(self) -> float:
        if self.exit_time is None:
            return 0.0
        
        if self.direction == 'LONG':
            return (self.exit_price - self.entry_price) * self.size
        else:
            return (self.entry_price - self.exit_price) * self.size
    
    @property
    def pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        
        if self.direction == 'LONG':
            return ((self.exit_price - self.entry_price) / self.entry_price) * 100
        else:
            return ((self.entry_price - self.exit_price) / self.entry_price) * 100
    
    @property
    def duration_minutes(self) -> float:
        if self.exit_time is None:
            return 0.0
        return (self.exit_time - self.entry_time).total_seconds() / 60


class BacktestResults:
    """Estadísticas del backtest"""
    def __init__(self, trades: List[BacktestTrade]):
        self.trades = trades
        self.closed_trades = [t for t in trades if t.exit_time is not None]
        
    @property
    def total_trades(self) -> int:
        return len(self.closed_trades)
    
    @property
    def winning_trades(self) -> int:
        return len([t for t in self.closed_trades if t.pnl > 0])
    
    @property
    def losing_trades(self) -> int:
        return len([t for t in self.closed_trades if t.pnl < 0])
    
    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return (self.winning_trades / self.total_trades) * 100
    
    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.closed_trades)
    
    @property
    def avg_win(self) -> float:
        if self.winning_trades == 0:
            return 0.0
        winning_pnls = [t.pnl for t in self.closed_trades if t.pnl > 0]
        return float(np.mean(winning_pnls))
    
    @property
    def avg_loss(self) -> float:
        if self.losing_trades == 0:
            return 0.0
        losing_pnls = [t.pnl for t in self.closed_trades if t.pnl < 0]
        return float(np.mean(losing_pnls))
    
    @property
    def profit_factor(self) -> float:
        total_wins = sum(t.pnl for t in self.closed_trades if t.pnl > 0)
        total_losses = abs(sum(t.pnl for t in self.closed_trades if t.pnl < 0))
        
        if total_losses == 0:
            return float('inf') if total_wins > 0 else 0.0
        return total_wins / total_losses
    
    @property
    def sharpe_ratio(self) -> float:
        if len(self.closed_trades) < 2:
            return 0.0
        
        returns = np.array([t.pnl_pct for t in self.closed_trades])
        if returns.std() == 0:
            return 0.0
        
        return (returns.mean() / returns.std()) * np.sqrt(252)
    
    @property
    def max_drawdown_pct(self) -> float:
        if len(self.closed_trades) == 0:
            return 0.0
        
        cumulative_pnl = np.cumsum([t.pnl for t in self.closed_trades])
        peak = 0.0
        max_dd = 0.0
        
        for pnl in cumulative_pnl:
            if pnl > peak:
                peak = pnl
            dd = peak - pnl
            if dd > max_dd:
                max_dd = dd
        
        if peak == 0:
            return 0.0
        return (max_dd / peak) * 100
    
    def to_dict(self) -> Dict:
        return {
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': f"{self.win_rate:.2f}%",
            'total_pnl': f"{self.total_pnl:.2f}",
            'avg_win': f"{self.avg_win:.2f}",
            'avg_loss': f"{self.avg_loss:.2f}",
            'profit_factor': f"{self.profit_factor:.2f}",
            'sharpe_ratio': f"{self.sharpe_ratio:.2f}",
            'max_drawdown_pct': f"{self.max_drawdown_pct:.2f}%",
        }


class ProfessionalBacktester:
    """
    Backtester que usa la ESTRATEGIA REAL de signal_engine.py
    
    Implementa:
    - HTF Filter (tendencia de largo plazo)
    - MTF Structure Filter (análisis multi-timeframe)
    - LTF Trigger Filter (entrada precisa)
    - Kill Conditions (protección)
    - Black Swan Filter (eventos macro)
    - Risk Engine (gestión de riesgo)
    """
    
    def __init__(self, symbol: str = 'BTC/USD', 
                 initial_balance: float = 10000.0,
                 position_size_pct: float = 1.0):
        self.symbol = symbol
        self.initial_balance = initial_balance
        self.position_size_pct = position_size_pct
        
        # Reset singletons for clean backtest
        reset_time_provider()
        
        # Initialize TimeProvider in backtest mode
        self._time_provider = get_time_provider(TimeProvider.MODE_BACKTEST)
        
        # Componentes - NOW USING BACKTEST MODE
        self.market_data = SynchronizedMultiTimeframeBuffer(symbol, backtest_mode=True)
        self.state_machine = StateMachine(backtest_mode=True)
        self.risk_engine = RiskEngine(capital=initial_balance)
        
        # Filtros (ESTRATEGIA REAL) - with backtest_mode flag
        self.kill_filter = KillConditionsFilter(self.state_machine, capital=initial_balance, backtest_mode=True)
        self.black_swan_filter = BlackSwanFilter()
        self.htf_filter = HTFFilter(backtest_mode=True)
        # MTFStructureFilter removed - FIX #1 enables earlier entries
        self.ltf_filter = LTFTriggerFilter(backtest_mode=True)
        
        # Edge Enforcement - INTEGRATED (was missing before)
        self.edge_enforcement = EdgeEnforcement(mode=EnforcementMode.BACKTEST)
        
        # Centralized Position Sizer
        self._position_sizer = get_position_sizer(
            risk_pct=0.01,
            max_position_pct=0.10,
            leverage=10
        )
        
        # Trading state
        self.trades: List[BacktestTrade] = []
        self.current_trade: Optional[BacktestTrade] = None
        self.candles_1m: List[Dict] = []
        
        logger.info(f"Professional Backtester initialized for {symbol} (with EdgeEnforcement)")
    
    def load_historical_candles(self, df: pd.DataFrame, timeframe: str = '1m') -> None:
        """Carga datos históricos y genera candles de otros timeframes"""
        df = df.copy()
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('timestamp')
        
        candles_1m = []
        for _, row in df.iterrows():
            candle = {
                'timestamp': row['timestamp'],
                'open': float(row['open']),
                'high': float(row['high']),
                'low': float(row['low']),
                'close': float(row['close']),
                'volume': float(row.get('volume', 0) or 0),
            }
            candles_1m.append(candle)
        
        self.candles_1m = candles_1m
        logger.info(f"Loaded {len(candles_1m)} 1m candles")
        
        # Generar candles de 5m a partir de 1m
        candles_5m = self._aggregate_candles(candles_1m, 5)
        logger.info(f"Generated {len(candles_5m)} 5m candles")
        
        # Generar candles de 1h a partir de 1m
        candles_1h = self._aggregate_candles(candles_1m, 60)
        logger.info(f"Generated {len(candles_1h)} 1h candles")
        
        # Pre-cargar el buffer con datos históricos
        for candle in candles_1m:
            candle['timeframe'] = '1m'
            self.market_data.candles['1m'].append(candle)
        
        for candle in candles_5m:
            candle['timeframe'] = '5m'
            self.market_data.candles['5m'].append(candle)
        
        for candle in candles_1h:
            candle['timeframe'] = '1h'
            self.market_data.candles['1h'].append(candle)
        
        logger.info("Historical data preloaded into buffer")
    
    def _aggregate_candles(self, candles: List[Dict], period_minutes: int) -> List[Dict]:
        """
        Agrega candles a un timeframe mayor
        Ej: 1m -> 5m, 1m -> 1h
        """
        if not candles:
            return []
        
        aggregated = []
        current_group = []
        current_period_start = None
        
        for candle in candles:
            timestamp = candle['timestamp']
            
            # Determinar el inicio del período
            if period_minutes == 5:
                period_start = timestamp.replace(minute=(timestamp.minute // 5) * 5, second=0, microsecond=0)
            elif period_minutes == 60:
                period_start = timestamp.replace(minute=0, second=0, microsecond=0)
            else:
                period_start = timestamp
            
            # Si es nuevo período, guardar el anterior
            if current_period_start is None:
                current_period_start = period_start
            elif period_start != current_period_start:
                if current_group:
                    aggregated.append(self._create_aggregated_candle(current_group))
                current_group = []
                current_period_start = period_start
            
            current_group.append(candle)
        
        # Guardar último grupo
        if current_group:
            aggregated.append(self._create_aggregated_candle(current_group))
        
        return aggregated
    
    def _create_aggregated_candle(self, candles: List[Dict]) -> Dict:
        """Crea un candle agregado a partir de múltiples candles"""
        if not candles:
            return {}
        
        return {
            'timestamp': candles[0]['timestamp'],
            'open': candles[0]['open'],
            'high': max(c['high'] for c in candles),
            'low': min(c['low'] for c in candles),
            'close': candles[-1]['close'],
            'volume': sum(c.get('volume', 0) for c in candles),
        }
    
    def _check_entry_signal_real_strategy(self, current_price: float, current_time: datetime) -> Tuple[Optional[str], Optional[Dict]]:
        """
        Implementa la ESTRATEGIA REAL usando los filtros de signal_engine.py
        
        Proceso:
        1. HTF Filter: Establecer bias de tendencia a largo plazo
        2. MTF Filter: Validar estructura con timeframes múltiples
        3. LTF Filter: Obtener señal de entrada precisa
        4. Risk Engine: Calcular stop loss y take profit
        """
        
        # Necesitamos suficientes candles
        if len(self.market_data.candles['1m']) < 20:
            return None, None
        
        candles_1m = list(self.market_data.candles['1m'])
        
        # ===== 1. HTF FILTER - Bias de tendencia (tendencia larga) =====
        # Necesitamos candles de 1h
        if len(self.market_data.candles['1h']) < 50:
            return None, None
        
        candles_1h = list(self.market_data.candles['1h'])
        closes_1h = [c['close'] for c in candles_1h[-50:]]
        
        # EMA 200 (tendencia larga)
        if len(closes_1h) >= 50:
            ema_200_1h = calculate_ema(np.array(closes_1h), 50)[-1]
            
            if current_price > ema_200_1h:
                htf_bias = 'LONG'
            elif current_price < ema_200_1h:
                htf_bias = 'SHORT'
            else:
                return None, None
        else:
            return None, None
        
        logger.debug(f"HTF Bias: {htf_bias} (Price: {current_price:.2f}, EMA200: {ema_200_1h:.2f})")
        
        # ===== 2. MTF FILTER - Estructura multiframe (5m) =====
        if len(self.market_data.candles['5m']) < 20:
            return None, None
        
        candles_5m = list(self.market_data.candles['5m'])
        closes_5m = np.array([c['close'] for c in candles_5m[-20:]])
        highs_5m = np.array([c['high'] for c in candles_5m[-20:]])
        lows_5m = np.array([c['low'] for c in candles_5m[-20:]])
        
        # EMA 20 (tendencia media)
        ema_20_5m = calculate_ema(closes_5m, 20)[-1]
        
        # ATR (volatilidad)
        atr_5m = calculate_atr(highs_5m, lows_5m, closes_5m, 14)[-1]
        
        logger.debug(f"MTF: EMA20_5m={ema_20_5m:.2f}, ATR={atr_5m:.2f}")
        
        # ===== 3. LTF TRIGGER - Entrada precisa (1m) =====
        closes_1m = np.array([c['close'] for c in candles_1m[-20:]])
        highs_1m = np.array([c['high'] for c in candles_1m[-20:]])
        lows_1m = np.array([c['low'] for c in candles_1m[-20:]])
        
        # EMA 5 y 10 (triggers rápidos)
        ema_5_1m = calculate_ema(closes_1m, 5)[-1] if len(closes_1m) >= 5 else None
        ema_10_1m = calculate_ema(closes_1m, 10)[-1] if len(closes_1m) >= 10 else None
        
        if ema_5_1m is None or ema_10_1m is None:
            return None, None
        
        atr_1m = calculate_atr(highs_1m, lows_1m, closes_1m, 14)[-1]
        
        # ===== LÓGICA DE ENTRADA REAL =====
        
        # LONG: HTF LONG + MTF válido + LTF trigger
        if htf_bias == 'LONG':
            # MTF validation: precio > EMA20 (con margen)
            if current_price >= ema_20_5m * 0.999:
                # LTF trigger: EMA5 > EMA10
                if ema_5_1m > ema_10_1m:
                    # Últimas velas alcistas (al menos 1 de 3)
                    recent_1m = candles_1m[-3:]
                    bullish = sum(1 for c in recent_1m if c['close'] > c['open'])
                    
                    if bullish >= 1:
                        # Stop loss: debajo del EMA10
                        stop_loss = ema_10_1m * 0.97
                        take_profit = current_price + (atr_1m * 2)
                        
                        logger.info(f"🔵 LONG SIGNAL: HTF={htf_bias}, Price>={ema_20_5m:.2f}, EMA5>EMA10, Bullish={bullish}")
                        
                        return 'LONG', {
                            'stop_loss': stop_loss,
                            'take_profit': take_profit,
                            'htf_bias': htf_bias,
                            'atr': atr_1m
                        }
        
        # SHORT: HTF SHORT + MTF válido + LTF trigger
        elif htf_bias == 'SHORT':
            # MTF validation: precio < EMA20 (con margen)
            if current_price <= ema_20_5m * 1.001:
                # LTF trigger: EMA5 < EMA10
                if ema_5_1m < ema_10_1m:
                    # Últimas velas bearish (al menos 1 de 3)
                    recent_1m = candles_1m[-3:]
                    bearish = sum(1 for c in recent_1m if c['close'] < c['open'])
                    
                    if bearish >= 1:
                        # Stop loss: arriba del EMA10
                        stop_loss = ema_10_1m * 1.03
                        take_profit = current_price - (atr_1m * 2)
                        
                        logger.info(f"🔴 SHORT SIGNAL: HTF={htf_bias}, Price<={ema_20_5m:.2f}, EMA5<EMA10, Bearish={bearish}")
                        
                        return 'SHORT', {
                            'stop_loss': stop_loss,
                            'take_profit': take_profit,
                            'htf_bias': htf_bias,
                            'atr': atr_1m
                        }
        
        return None, None
    
    def _check_exit_conditions(self, current_price: float, current_time: datetime) -> Tuple[Optional[float], str]:
        """Verifica condiciones de salida"""
        if self.current_trade is None:
            return None, ""
        
        trade = self.current_trade
        
        # Stop Loss (máxima prioridad)
        if trade.direction == 'LONG' and current_price <= trade.stop_loss:
            return trade.stop_loss, 'SL'
        elif trade.direction == 'SHORT' and current_price >= trade.stop_loss:
            return trade.stop_loss, 'SL'
        
        # Take Profit
        tp = trade.entry_params.get('take_profit')
        if tp:
            if trade.direction == 'LONG' and current_price >= tp:
                return tp, 'TP'
            elif trade.direction == 'SHORT' and current_price <= tp:
                return tp, 'TP'
        
        # Timeout (5 minutos)
        duration = (current_time - trade.entry_time).total_seconds() / 60
        if duration > 5:
            return current_price, 'TIMEOUT'
        
        return None, ""
    
    def run(self, start_date: Optional[datetime] = None, 
            end_date: Optional[datetime] = None) -> BacktestResults:
        """Ejecuta el backtest con estrategia real"""
        
        if not self.candles_1m:
            raise ValueError("No candles loaded")
        
        logger.info("Starting professional backtest with REAL strategy...")
        logger.info(f"Buffer status: 1m={len(self.market_data.candles['1m'])}, "
                   f"5m={len(self.market_data.candles['5m'])}, "
                   f"1h={len(self.market_data.candles['1h'])}")
        
        for i, candle in enumerate(self.candles_1m):
            timestamp = candle['timestamp']
            close_price = candle['close']
            
            if start_date and timestamp < start_date:
                continue
            if end_date and timestamp > end_date:
                break
            
            # Los candles ya están en el buffer (precargados)
            # Solo necesitamos procesarlos para los indicadores
            
            # Update last price
            self.market_data.last_price = close_price
            
            # Check exit
            if self.current_trade:
                exit_price, exit_reason = self._check_exit_conditions(close_price, timestamp)
                if exit_price:
                    self.current_trade.exit_time = timestamp
                    self.current_trade.exit_price = exit_price
                    self.current_trade.exit_reason = exit_reason
                    self.trades.append(self.current_trade)
                    logger.info(f"✅ Trade closed: {exit_reason} at {exit_price:.2f} (P&L: {self.current_trade.pnl:.2f})")
                    self.current_trade = None
            
            # Check entry
            if not self.current_trade:
                direction, entry_params = self._check_entry_signal_real_strategy(close_price, timestamp)
                
                if direction and entry_params:
                    position_size = (self.initial_balance * self.position_size_pct / 100) / close_price
                    
                    self.current_trade = BacktestTrade(
                        entry_time=timestamp,
                        entry_price=close_price,
                        direction=direction,
                        size=position_size,
                        stop_loss=entry_params['stop_loss'],
                        entry_params=entry_params
                    )
                    self.current_trade.htf_bias = entry_params.get('htf_bias', '')
                    
                    logger.info(f"📊 Trade opened: {direction} at {close_price:.2f}")
        
        # Close open trade at end
        if self.current_trade:
            last_candle = self.candles_1m[-1]
            self.current_trade.exit_time = last_candle['timestamp']
            self.current_trade.exit_price = last_candle['close']
            self.current_trade.exit_reason = 'BACKTEST_END'
            self.trades.append(self.current_trade)
        
        logger.info(f"✅ Backtest completed")
        return BacktestResults(self.trades)
    
    def get_report(self) -> str:
        """Genera reporte"""
        if not self.trades:
            return "No trades executed"
        
        results = BacktestResults(self.trades)
        report = "\n" + "="*70 + "\n"
        report += "PROFESSIONAL BACKTEST REPORT (REAL STRATEGY)\n"
        report += "="*70 + "\n"
        
        for key, value in results.to_dict().items():
            report += f"{key:.<40} {value:>20}\n"
        
        report += "="*70 + "\n"
        
        return report
