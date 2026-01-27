"""
Backtester Module - Simula el trading system con datos históricos
Calcula métricas de rendimiento y valida la estrategia
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta, UTC
from typing import Dict, List, Tuple, Optional
from collections import deque
import logging

from src.engine.market_data import SynchronizedMultiTimeframeBuffer
from src.engine.filters import (
    KillConditionsFilter, BlackSwanFilter, HTFFilter,
    MTFStructureFilter, LTFTriggerFilter
)
from src.engine.risk_engine import RiskEngine
from src.engine.state_machine import StateMachine, BotState
from src.engine.indicators import (
    calculate_ema, calculate_atr, calculate_adx,
    calculate_relative_volume, calculate_orderbook_imbalance
)

logger = logging.getLogger(__name__)


# ============================================================================
# TRADING COSTS CONFIGURATION
# ============================================================================
class TradingCosts:
    """Realistas para Kraken Futures"""
    COMMISSION_MAKER = 0.0002        # 0.02% (taker/post-only)
    COMMISSION_TAKER = 0.0005        # 0.05% (aggressive fill)
    FUNDING_RATE_DAILY = 0.0001      # ~0.01% por 8h (3x por día)
    SLIPPAGE_NORMAL = 0.0005         # 0.05% (spread + execution)
    SLIPPAGE_MOMENTUM = 0.002        # 0.2% (en movimientos rápidos)
    
    @staticmethod
    def get_entry_commission(is_post_only: bool = True) -> float:
        """Comisión en entrada (post-only vs market)"""
        return TradingCosts.COMMISSION_MAKER if is_post_only else TradingCosts.COMMISSION_TAKER
    
    @staticmethod
    def get_exit_commission(duration_minutes: float) -> float:
        """Comisión en salida + funding acumulado"""
        # Comisión maker (asumimos exit post-only o market)
        commission = TradingCosts.COMMISSION_MAKER
        
        # Funding: ~0.01% por 8h = 0.03% por día
        funding_hours = duration_minutes / 60
        funding = (funding_hours / 8) * TradingCosts.FUNDING_RATE_DAILY * 3
        
        return commission + funding


class BacktestTrade:
    """Representa un trade en el backtest con cálculo de costos reales"""
    def __init__(self, entry_time: datetime, entry_price: float, 
                 direction: str, size: float, stop_loss: float, entry_params: Dict = None,
                 post_only: bool = True, momentum_filled: bool = False):
        self.entry_time = entry_time
        self.entry_price = entry_price
        self.direction = direction  # 'LONG' o 'SHORT'
        self.size = size
        self.stop_loss = stop_loss
        self.entry_params = entry_params or {}
        
        # Cost tracking
        self.post_only = post_only  # Entrada post-only o market?
        self.momentum_filled = momentum_filled  # Entró en movimiento rápido?
        
        # Calcular comisión de entrada
        self.entry_commission = self._calculate_entry_cost()
        self.exit_commission: float = 0.0
        self.total_costs: float = 0.0
        
        # Exit info
        self.exit_time: Optional[datetime] = None
        self.exit_price: float = 0.0
        self.exit_reason: str = ""  # 'TP', 'SL', 'EXIT_SIGNAL', 'TIMEOUT'
    
    def _calculate_entry_cost(self) -> float:
        """Calcula costo de entrada (comisión + slippage)"""
        commission = TradingCosts.get_entry_commission(self.post_only)
        slippage = TradingCosts.SLIPPAGE_MOMENTUM if self.momentum_filled else TradingCosts.SLIPPAGE_NORMAL
        entry_cost_pct = commission + slippage
        
        # Costo en USD
        notional = self.entry_price * self.size
        return notional * entry_cost_pct
    
    def _calculate_exit_cost(self) -> float:
        """Calcula costo de salida (comisión + funding)"""
        if self.exit_time is None:
            return 0.0
        
        duration_minutes = (self.exit_time - self.entry_time).total_seconds() / 60
        exit_cost_pct = TradingCosts.get_exit_commission(duration_minutes)
        
        # Costo en USD
        notional = self.exit_price * self.size
        return notional * exit_cost_pct
    
    def close_trade(self, exit_time: datetime, exit_price: float, exit_reason: str):
        """Registra el cierre del trade"""
        self.exit_time = exit_time
        self.exit_price = exit_price
        self.exit_reason = exit_reason
        self.exit_commission = self._calculate_exit_cost()
        self.total_costs = self.entry_commission + self.exit_commission
        
    @property
    def pnl_gross(self) -> float:
        """PnL sin costos (como antes)"""
        if self.exit_time is None:
            return 0.0
        
        if self.direction == 'LONG':
            return (self.exit_price - self.entry_price) * self.size
        else:  # SHORT
            return (self.entry_price - self.exit_price) * self.size
    
    @property
    def pnl(self) -> float:
        """PnL neto (CON costos)"""
        if self.exit_time is None:
            return 0.0
        
        return self.pnl_gross - self.total_costs
    
    @property
    def pnl_pct(self) -> float:
        """PnL % neto (CON costos)"""
        if self.entry_price == 0:
            return 0.0
        
        if self.direction == 'LONG':
            return ((self.exit_price - self.entry_price) / self.entry_price) * 100 - (self.total_costs / (self.entry_price * self.size)) * 100
        else:  # SHORT
            return ((self.entry_price - self.exit_price) / self.entry_price) * 100 - (self.total_costs / (self.entry_price * self.size)) * 100
    
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
    def breakeven_trades(self) -> int:
        return len([t for t in self.closed_trades if t.pnl == 0])
    
    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return (self.winning_trades / self.total_trades) * 100
    
    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.closed_trades)
    
    @property
    def total_pnl_pct(self) -> float:
        if len(self.closed_trades) == 0:
            return 0.0
        avg_entry = np.mean([t.entry_price for t in self.closed_trades])
        return (self.total_pnl / (avg_entry * sum(t.size for t in self.closed_trades))) * 100
    
    @property
    def avg_win(self) -> float:
        if self.winning_trades == 0:
            return 0.0
        winning_pnls = [t.pnl for t in self.closed_trades if t.pnl > 0]
        return np.mean(winning_pnls)
    
    @property
    def avg_loss(self) -> float:
        if self.losing_trades == 0:
            return 0.0
        losing_pnls = [t.pnl for t in self.closed_trades if t.pnl < 0]
        return np.mean(losing_pnls)
    
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
        
        return (returns.mean() / returns.std()) * np.sqrt(252)  # Annualized
    
    @property
    def max_consecutive_losses(self) -> int:
        if len(self.closed_trades) == 0:
            return 0
        
        max_streak = 0
        current_streak = 0
        
        for trade in self.closed_trades:
            if trade.pnl < 0:
                current_streak += 1
                max_streak = max(max_streak, current_streak)
            else:
                current_streak = 0
        
        return max_streak
    
    @property
    def max_consecutive_wins(self) -> int:
        if len(self.closed_trades) == 0:
            return 0
        
        max_streak = 0
        current_streak = 0
        
        for trade in self.closed_trades:
            if trade.pnl > 0:
                current_streak += 1
                max_streak = max(max_streak, current_streak)
            else:
                current_streak = 0
        
        return max_streak
    
    @property
    def max_drawdown(self) -> float:
        if len(self.closed_trades) == 0:
            return 0.0
        
        cumulative_pnl = np.cumsum([t.pnl for t in self.closed_trades])
        running_max = np.maximum.accumulate(cumulative_pnl)
        drawdowns = cumulative_pnl - running_max
        
        return float(np.min(drawdowns))
    
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
    
    @property
    def avg_trade_duration_minutes(self) -> float:
        if len(self.closed_trades) == 0:
            return 0.0
        return np.mean([t.duration_minutes for t in self.closed_trades])
    
    @property
    def total_costs(self) -> float:
        """Costo total de comisiones + funding + slippage"""
        return sum(t.total_costs for t in self.closed_trades)
    
    @property
    def total_pnl_gross(self) -> float:
        """PnL sin costos (para comparación)"""
        return sum(t.pnl_gross for t in self.closed_trades)
    
    def get_cost_analysis(self) -> Dict:
        """Análisis detallado de costos"""
        total_entries = sum(t.entry_commission for t in self.closed_trades)
        total_exits = sum(t.exit_commission for t in self.closed_trades)
        
        return {
            'total_entry_costs': f"${total_entries:.2f}",
            'total_exit_costs': f"${total_exits:.2f}",
            'total_costs': f"${self.total_costs:.2f}",
            'cost_per_trade_avg': f"${self.total_costs / self.total_trades if self.total_trades > 0 else 0:.2f}",
            'pnl_gross': f"${self.total_pnl_gross:.2f}",
            'pnl_net': f"${self.total_pnl:.2f}",
            'costs_as_pct_gross': f"{(self.total_costs / abs(self.total_pnl_gross) * 100) if self.total_pnl_gross != 0 else 0:.2f}%"
        }
    
    def to_dict(self) -> Dict:
        """Convierte resultados a diccionario para reporte"""
        return {
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'breakeven_trades': self.breakeven_trades,
            'win_rate': f"{self.win_rate:.2f}%",
            'total_pnl': f"${self.total_pnl:.2f}",
            'total_pnl_pct': f"{self.total_pnl_pct:.2f}%",
            'avg_win': f"${self.avg_win:.2f}",
            'avg_loss': f"${self.avg_loss:.2f}",
            'profit_factor': f"{self.profit_factor:.2f}",
            'sharpe_ratio': f"{self.sharpe_ratio:.2f}",
            'max_consecutive_wins': self.max_consecutive_wins,
            'max_consecutive_losses': self.max_consecutive_losses,
            'max_drawdown': f"${self.max_drawdown:.2f}",
            'max_drawdown_pct': f"{self.max_drawdown_pct:.2f}%",
            'avg_trade_duration_minutes': f"{self.avg_trade_duration_minutes:.2f}",
        }


class Backtester:
    """
    Motor de backtesting
    Simula el trading system completo con datos históricos
    """
    
    def __init__(self, symbol: str = 'BTC/USD', 
                 initial_balance: float = 10000.0,
                 position_size_pct: float = 1.0,
                 entry_timeout_minutes: int = 3):
        """
        Args:
            symbol: Símbolo del trading (ej: 'BTC/USD')
            initial_balance: Balance inicial para cálculos
            position_size_pct: Porcentaje del balance por trade (1.0 = 1%)
            entry_timeout_minutes: Minutos antes de cancelar entrada pendiente
        """
        self.symbol = symbol
        self.initial_balance = initial_balance
        self.position_size_pct = position_size_pct
        self.entry_timeout_minutes = entry_timeout_minutes
        
        # Componentes del sistema
        self.market_data = SynchronizedMultiTimeframeBuffer(symbol)
        self.state_machine = StateMachine()
        self.risk_engine = RiskEngine(capital=initial_balance)
        
        # Filtros
        self.kill_filter = KillConditionsFilter(self.state_machine, capital=initial_balance)
        self.black_swan_filter = BlackSwanFilter()
        self.htf_filter = HTFFilter()
        self.mtf_filter = MTFStructureFilter()
        self.ltf_filter = LTFTriggerFilter()
        
        # Trading state
        self.trades: List[BacktestTrade] = []
        self.current_trade: Optional[BacktestTrade] = None
        self.current_bias: Optional[str] = None
        self.pending_entry_time: Optional[datetime] = None
        self.pending_direction: Optional[str] = None
        
        # Historical data
        self.candles_1h: List[Dict] = []
        self.candles_5m: List[Dict] = []
        self.candles_1m: List[Dict] = []
        
        logger.info(f"Backtester initialized for {symbol} with balance ${initial_balance}")
    
    def load_historical_candles(self, df: pd.DataFrame, timeframe: str = '1m') -> None:
        """
        Carga datos históricos desde un DataFrame
        
        Args:
            df: DataFrame con columnas [timestamp, open, high, low, close, volume]
            timeframe: '1m', '5m', o '1h'
        """
        df = df.copy()
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('timestamp')
        
        # Convertir a lista de dicts
        candles = []
        for _, row in df.iterrows():
            candle = {
                'timestamp': row['timestamp'],
                'open': float(row['open']),
                'high': float(row['high']),
                'low': float(row['low']),
                'close': float(row['close']),
                'volume': float(row.get('volume', 0)),
            }
            candles.append(candle)
        
        if timeframe == '1m':
            self.candles_1m = candles
        elif timeframe == '5m':
            self.candles_5m = candles
        elif timeframe == '1h':
            self.candles_1h = candles
        
        logger.info(f"Loaded {len(candles)} {timeframe} candles")
    
    def _process_candle(self, candle: Dict, timeframe: str) -> None:
        """Procesa una vela cerrada"""
        # Agregar timeframe al candle
        candle['timeframe'] = timeframe
        # Agregar a buffer
        self.market_data.add_candle(candle)
    
    def _check_entry_signal(self, current_price: float, current_time: datetime) -> Tuple[Optional[str], Optional[Dict]]:
        """
        Verifica si hay señal de entrada
        Estrategia mejorada con análisis de tendencia, volatilidad y filtros
        Retorna: (dirección, entry_params) o (None, None)
        """
        # Necesitamos al menos 20 candles para análisis
        if len(self.market_data.candles['1m']) < 20:
            return None, None
        
        candles_1m = list(self.market_data.candles['1m'])
        
        # ===== ANÁLISIS DE VOLATILIDAD =====
        # Calcular volatilidad de últimas 10 velas
        recent_closes = [c['close'] for c in candles_1m[-10:]]
        volatility = np.std(recent_closes) / np.mean(recent_closes) if recent_closes else 0
        
        # Si volatilidad muy alta, no entrar (mercado inestable)
        if volatility > 0.05:
            return None, None
        
        # ===== ANÁLISIS DE TENDENCIA =====
        # EMA rápida (5) y lenta (10)
        closes_20 = [c['close'] for c in candles_1m[-20:]]
        ema5 = self._calculate_ema(closes_20, 5)
        ema10 = self._calculate_ema(closes_20, 10)
        
        # Necesitamos EMAs válidas
        if ema5 is None or ema10 is None:
            return None, None
        
        # ===== SEÑAL LONG =====
        # Condiciones:
        # 1. EMA5 > EMA10 (tendencia alcista)
        # 2. Últimas 3 velas bullish (momentum)
        # 3. Precio está al alza
        recent_3 = candles_1m[-3:]
        bullish_count_3 = sum(1 for c in recent_3 if c['close'] > c['open'])
        
        if ema5 > ema10 * 1.001 and bullish_count_3 >= 2:  # 1.001 = 0.1% tolerancia
            # Precio sube respecto a EMA
            if current_price >= ema5:
                # Calcular stop loss dinámico (ATR-based)
                recent_10_lows = [c['low'] for c in candles_1m[-10:]]
                stop_loss = min(recent_10_lows) * 0.995  # 0.5% debajo del low reciente
                
                # Take profit basado en volatilidad
                atr = np.mean([c['high'] - c['low'] for c in candles_1m[-10:]])
                take_profit = current_price + (atr * 2)  # 2x ATR
                
                logger.info(f"LONG signal: EMA5={ema5:.2f} > EMA10={ema10:.2f}, Vol={volatility:.4f}")
                
                return 'LONG', {
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                    'reason': 'EMA_CROSS_BULLISH'
                }
        
        # ===== SEÑAL SHORT =====
        # Condiciones:
        # 1. EMA5 < EMA10 (tendencia bajista)
        # 2. Últimas 3 velas bearish (momentum)
        # 3. Precio está a la baja
        bearish_count_3 = sum(1 for c in recent_3 if c['close'] < c['open'])
        
        if ema5 < ema10 * 0.999 and bearish_count_3 >= 2:
            # Precio baja respecto a EMA
            if current_price <= ema5:
                # Calcular stop loss dinámico
                recent_10_highs = [c['high'] for c in candles_1m[-10:]]
                stop_loss = max(recent_10_highs) * 1.005  # 0.5% arriba del high reciente
                
                # Take profit basado en volatilidad
                atr = np.mean([c['high'] - c['low'] for c in candles_1m[-10:]])
                take_profit = current_price - (atr * 2)  # 2x ATR
                
                logger.info(f"SHORT signal: EMA5={ema5:.2f} < EMA10={ema10:.2f}, Vol={volatility:.4f}")
                
                return 'SHORT', {
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                    'reason': 'EMA_CROSS_BEARISH'
                }
        
        return None, None
    
    def _calculate_ema(self, prices: List[float], period: int) -> Optional[float]:
        """Calcula EMA de una lista de precios"""
        if len(prices) < period:
            return None
        
        # EMA simple
        multiplier = 2 / (period + 1)
        ema = np.mean(prices[:period])  # SMA inicial
        
        for price in prices[period:]:
            ema = price * multiplier + ema * (1 - multiplier)
        
        return ema
    
    def _check_exit_conditions(self, current_price: float, current_time: datetime) -> Tuple[Optional[str], str]:
        """
        Verifica condiciones de salida del trade actual
        Retorna: (precio_exit, motivo) o (None, "")
        """
        if self.current_trade is None:
            return None, ""
        
        trade = self.current_trade
        exit_price = None
        exit_reason = ""
        
        # ===== CHECK STOP LOSS (máxima prioridad) =====
        if trade.direction == 'LONG':
            if current_price <= trade.stop_loss:
                exit_price = trade.stop_loss
                exit_reason = 'SL'
        else:  # SHORT
            if current_price >= trade.stop_loss:
                exit_price = trade.stop_loss
                exit_reason = 'SL'
        
        # ===== CHECK TAKE PROFIT =====
        if exit_price is None:
            # Usar take profit calculado dinámicamente
            tp = trade.entry_params.get('take_profit') if hasattr(trade, 'entry_params') else None
            
            if tp is not None:
                if trade.direction == 'LONG' and current_price >= tp:
                    exit_price = tp
                    exit_reason = 'TP'
                elif trade.direction == 'SHORT' and current_price <= tp:
                    exit_price = tp
                    exit_reason = 'TP'
        
        # ===== CHECK TIMEOUT (3 minutos) =====
        if exit_price is None:
            duration = (current_time - trade.entry_time).total_seconds() / 60
            if duration > self.entry_timeout_minutes:
                exit_price = current_price
                exit_reason = 'TIMEOUT'
        
        return exit_price, exit_reason
    
    def run(self, start_date: Optional[datetime] = None, 
            end_date: Optional[datetime] = None) -> BacktestResults:
        """
        Ejecuta el backtest con los datos históricos cargados
        
        Args:
            start_date: Fecha inicial (opcional)
            end_date: Fecha final (opcional)
        
        Returns:
            BacktestResults con estadísticas
        """
        if not self.candles_1m:
            raise ValueError("No 1m candles loaded. Call load_historical_candles() first.")
        
        logger.info("Starting backtest...")
        start_time = datetime.now()
        
        for candle in self.candles_1m:
            timestamp = candle['timestamp']
            close_price = candle['close']
            
            # Check de rango de fechas
            if start_date and timestamp < start_date:
                continue
            if end_date and timestamp > end_date:
                break
            
            # Procesar vela
            self._process_candle(candle, '1m')
            
            # Check exit del trade actual
            if self.current_trade is not None:
                exit_price, exit_reason = self._check_exit_conditions(close_price, timestamp)
                if exit_price is not None:
                    self.current_trade.exit_time = timestamp
                    self.current_trade.exit_price = exit_price
                    self.current_trade.exit_reason = exit_reason
                    self.trades.append(self.current_trade)
                    logger.info(f"Trade closed: {exit_reason} at {exit_price} (P&L: {self.current_trade.pnl:.2f})")
                    self.current_trade = None
            
            # Check entry signal si no hay trade abierto
            if self.current_trade is None:
                direction, entry_params = self._check_entry_signal(close_price, timestamp)
                
                if direction is not None:
                    # Calcular tamaño de posición
                    position_size = (self.initial_balance * self.position_size_pct / 100) / close_price
                    
                    self.current_trade = BacktestTrade(
                        entry_time=timestamp,
                        entry_price=close_price,
                        direction=direction,
                        size=position_size,
                        stop_loss=entry_params['stop_loss'],
                        entry_params=entry_params
                    )
                    logger.info(f"Trade opened: {direction} at {close_price}, SL: {entry_params['stop_loss']:.2f}, TP: {entry_params.get('take_profit', 0):.2f}")
        
        # Cerrar trade abierto al final si existe
        if self.current_trade is not None:
            last_candle = self.candles_1m[-1]
            self.current_trade.exit_time = last_candle['timestamp']
            self.current_trade.exit_price = last_candle['close']
            self.current_trade.exit_reason = 'BACKTEST_END'
            self.trades.append(self.current_trade)
        
        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info(f"Backtest completed in {elapsed:.2f}s")
        
        return BacktestResults(self.trades)
    
    def get_report(self) -> str:
        """Genera reporte en texto"""
        if not self.trades:
            return "No trades executed"
        
        results = BacktestResults(self.trades)
        report = "\n" + "="*70 + "\n"
        report += "BACKTEST REPORT\n"
        report += "="*70 + "\n"
        
        for key, value in results.to_dict().items():
            report += f"{key:.<40} {value:>20}\n"
        
        report += "="*70 + "\n\n"
        report += "TRADES:\n"
        report += "-"*70 + "\n"
        
        for i, trade in enumerate(results.closed_trades, 1):
            report += f"\nTrade #{i}\n"
            report += f"  Direction:     {trade.direction}\n"
            report += f"  Entry:         {trade.entry_price:.2f} @ {trade.entry_time}\n"
            report += f"  Exit:          {trade.exit_price:.2f} @ {trade.exit_time} ({trade.exit_reason})\n"
            report += f"  P&L:           {trade.pnl:.2f} ({trade.pnl_pct:.2f}%)\n"
            report += f"  Duration:      {trade.duration_minutes:.0f} minutes\n"
        
        return report
