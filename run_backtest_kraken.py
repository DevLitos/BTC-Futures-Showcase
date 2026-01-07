#!/usr/bin/env python3
"""
Backtest usando las clases REALES del sistema (SignalEngine, RiskEngine, etc.)
Descarga datos históricos de Kraken Futures y simula trading.

Uso:
    python run_backtest_kraken.py --days 7
    python run_backtest_kraken.py --days 30 --symbol PF_XBTUSD
"""

import asyncio
import logging
import argparse
from datetime import datetime, timedelta, UTC
from typing import Optional, Dict, List, Tuple
import pandas as pd
import numpy as np
import ccxt

# Importar clases REALES del sistema
from engine.signal_engine import SignalEngine
from engine.market_data import SynchronizedMultiTimeframeBuffer
from engine.risk_engine import RiskEngine
from engine.state_machine import BotState

# Import validation framework (optional slippage simulation)
try:
    from engine.backtest_validation import (
        SlippageSimulator, SlippageConfig, FeatureFlag, is_feature_enabled
    )
    VALIDATION_AVAILABLE = True
except ImportError:
    VALIDATION_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)-8s - %(message)s'
)
logger = logging.getLogger('BacktestKraken')


# =============================================================================
# MOCK CLASSES - Simulan exchange sin conexión real
# =============================================================================

class MockExecution:
    """Simula ejecución de órdenes para backtest"""
    
    def __init__(self, enable_slippage: bool = False):
        self.orders: Dict[str, dict] = {}
        self.order_counter = 0
        self.position: Optional[dict] = None
        self.current_price = 0.0
        self.fills: List[dict] = []
        self.open_orders: Dict[str, dict] = {}
        self.pending_sl: Optional[float] = None
        self.pending_tp: Optional[float] = None
        
        # Slippage simulation (more conservative backtests)
        self.enable_slippage = enable_slippage
        if enable_slippage and VALIDATION_AVAILABLE:
            self.slippage_sim = SlippageSimulator()
        else:
            self.slippage_sim = None
        
        # Track last direction for slippage calculation
        self._last_direction: str = "LONG"
        
    def set_price(self, price: float):
        """Actualiza precio actual (llamado cada tick)"""
        self.current_price = price
        self._check_fills()
    
    def _check_fills(self):
        """Verifica si órdenes limit se llenan"""
        to_remove = []
        for order_id, order in self.open_orders.items():
            if order['type'] == 'limit':
                if order['side'] == 'buy' and self.current_price <= order['price']:
                    self._fill_order(order_id, order)
                    to_remove.append(order_id)
                elif order['side'] == 'sell' and self.current_price >= order['price']:
                    self._fill_order(order_id, order)
                    to_remove.append(order_id)
        for oid in to_remove:
            del self.open_orders[oid]
    
    def _fill_order(self, order_id: str, order: dict):
        """Registra fill de orden"""
        # Apply slippage if enabled (makes backtest more conservative)
        fill_price = self.current_price
        if self.slippage_sim:
            is_entry = self.position is None
            direction = "LONG" if order['side'] == 'buy' else "SHORT"
            is_market = order.get('type') == 'market'
            
            fill_price = self.slippage_sim.apply_slippage(
                price=self.current_price,
                direction=direction,
                is_entry=is_entry,
                is_market_order=is_market
            )
            self._last_direction = direction
        
        self.fills.append({
            'order_id': order_id,
            'price': fill_price,
            'amount': order['amount'],
            'side': order['side'],
            'timestamp': datetime.now(UTC)
        })
        
        # Actualizar posición - usar formato compatible con el código real
        if order['side'] == 'buy':
            if self.position is None:
                self.position = {
                    'contracts': order['amount'], 
                    'size': order['amount'], 
                    'side': 'long', 
                    'entry': fill_price,
                    'entryPrice': fill_price
                }
            else:
                self.position['contracts'] += order['amount']
                self.position['size'] += order['amount']
        else:  # sell
            if self.position is None:
                self.position = {
                    'contracts': -order['amount'],  # Negative for short
                    'size': order['amount'], 
                    'side': 'short', 
                    'entry': fill_price,
                    'entryPrice': fill_price
                }
            else:
                self.position['contracts'] -= order['amount']
                self.position['size'] -= order['amount']
                if abs(self.position['contracts']) < 0.0001:
                    self.position = None

    async def initialize(self):
        pass
    
    async def close(self):
        pass
    
    async def place_limit_order(self, symbol: str, side: str, amount: float, 
                                price: float, post_only: bool = True,
                                stop_loss: float = None, take_profit: float = None) -> dict:
        self.order_counter += 1
        order_id = f"backtest_order_{self.order_counter}"
        
        # Store SL/TP levels for later use
        self.pending_sl = stop_loss
        self.pending_tp = take_profit
        
        # Fill limit order immediately for backtest (simulates instant fill)
        # NOTE: Do NOT add to open_orders to avoid double fill in _check_fills
        order = {'side': side, 'amount': amount, 'type': 'limit', 'price': price}
        self._fill_order(order_id, order)
        
        return {'id': order_id, 'status': 'filled', 'filled': amount, 'price': price, 'average': price}
    
    async def place_market_order(self, symbol: str, side: str, amount: float,
                                  stop_loss: float = None, take_profit: float = None) -> dict:
        self.order_counter += 1
        order_id = f"backtest_market_{self.order_counter}"
        
        # Store SL/TP levels
        self.pending_sl = stop_loss
        self.pending_tp = take_profit
        
        # Market order se llena inmediatamente
        self._fill_order(order_id, {'side': side, 'amount': amount, 'type': 'market'})
        
        # Return order info dict (not just string)
        return {
            'id': order_id,
            'status': 'closed',
            'filled': amount,
            'price': self.current_price,
            'side': side,
            'amount': amount
        }
    
    async def close_position_market(self, side: str, amount: float) -> bool:
        if self.position:
            self.position['size'] -= amount
            if self.position['size'] <= 0:
                self.position = None
        return True
    
    async def cancel_order(self, order_id: str) -> bool:
        if order_id in self.open_orders:
            del self.open_orders[order_id]
            return True
        return False
    
    async def cancel_all_orders(self, symbol: str) -> int:
        count = len(self.open_orders)
        self.open_orders.clear()
        return count
    
    async def get_open_orders(self, symbol: str) -> List[dict]:
        return list(self.open_orders.values())
    
    async def poll_fill_status(self, order_id: str, symbol: str) -> Optional[dict]:
        for fill in self.fills:
            if fill['order_id'] == order_id:
                return {'price': fill['price'], 'amount': fill['amount'], 'filled': True}
        if order_id in self.open_orders:
            return None  # Still open
        return None
    
    async def has_open_position(self, symbol: str) -> bool:
        has_pos = self.position is not None and self.position['size'] > 0
        logger.info(f"[MOCK] has_open_position({symbol}): {has_pos} | position={self.position}")
        return has_pos
    
    async def get_position(self, symbol: str) -> Optional[dict]:
        return self.position
    
    async def set_position_stops(self, symbol: str, stop_loss: float, 
                                  take_profit: float = None) -> bool:
        return True  # Backtest maneja SL/TP internamente via RiskEngine
    
    async def verify_stops_on_exchange(self, symbol: str) -> dict:
        return {'has_sl': True, 'has_tp': True}
    
    async def get_order_status(self, order_id: str, symbol: str) -> Optional[dict]:
        if order_id in self.open_orders:
            return self.open_orders[order_id]
        return None
    
    async def get_available_capital(self) -> float:
        """Return available capital for position sizing"""
        return 1000.0  # Fixed capital for backtest (1000 EUR)
    
    async def get_account_balance(self) -> dict:
        """Return mock account balance for backtest"""
        return {'total': 10000.0, 'free': 10000.0, 'used': 0.0}


class MockExternalData:
    """Simula datos externos (funding rate, etc.)"""
    
    def __init__(self):
        self.funding_rate = 0.0001  # 0.01% default
        self.macro = None
    
    async def get_funding_rate(self, symbol: str) -> float:
        return self.funding_rate
    
    async def fetch_funding_rate(self, symbol: str) -> float:
        return self.funding_rate
    
    def can_trade(self) -> Tuple[bool, str]:
        """
        Backtest always allows trading (no external data restrictions)
        """
        return True, "Backtest mode - external data OK"


class MockTradeLogger:
    """Logger de trades para backtest"""
    
    def __init__(self):
        self.events: List[dict] = []
        self.trades: List[dict] = []
    
    async def log_event(self, event_type: str, data: dict):
        event = {'type': event_type, 'data': data, 'timestamp': datetime.now(UTC)}
        self.events.append(event)
        
        if event_type in ['trade_opened', 'trade_closed']:
            self.trades.append(event)
            logger.info(f"📝 {event_type}: {data}")


# =============================================================================
# DATA LOADER - Descarga de Kraken
# =============================================================================

def download_kraken_data(symbol: str = 'BTC/USD:USD', days: int = 7, offset_days: int = 0) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Descarga datos históricos de Kraken Futures
    Retorna DataFrames para 1m, 5m, 1h
    
    Args:
        symbol: Trading pair
        days: Number of days to download
        offset_days: Offset from today (0=last N days, 7=from 7 days ago to 14 days ago)
    """
    if offset_days > 0:
        logger.info(f"📥 Downloading {days} days of data for {symbol} (offset: -{offset_days} days)...")
    else:
        logger.info(f"📥 Downloading {days} days of data for {symbol}...")
    
    # Usar Kraken Futures
    exchange = ccxt.krakenfutures({
        'enableRateLimit': True,
    })
    
    end_date = datetime.now(UTC) - timedelta(days=offset_days)
    start_date = end_date - timedelta(days=days)
    since = exchange.parse8601(start_date.isoformat())
    
    def fetch_all_ohlcv(tf: str) -> pd.DataFrame:
        ohlcv = []
        fetch_since = since
        limit = 720  # Kraken limit
        
        while fetch_since < exchange.milliseconds():
            try:
                chunk = exchange.fetch_ohlcv(symbol, tf, fetch_since, limit)
                if not chunk:
                    break
                ohlcv.extend(chunk)
                fetch_since = chunk[-1][0] + 1
                logger.debug(f"  Fetched {len(chunk)} {tf} candles...")
            except Exception as e:
                logger.warning(f"  Error fetching {tf}: {e}")
                break
        
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        return df
    
    df_1m = fetch_all_ohlcv('1m')
    logger.info(f"  ✅ 1m: {len(df_1m)} candles")
    
    df_5m = fetch_all_ohlcv('5m')
    logger.info(f"  ✅ 5m: {len(df_5m)} candles")
    
    df_1h = fetch_all_ohlcv('1h')
    logger.info(f"  ✅ 1h: {len(df_1h)} candles")
    
    return df_1m, df_5m, df_1h


# =============================================================================
# BACKTEST RUNNER
# =============================================================================

class BacktestRunner:
    """Ejecuta backtest usando las clases REALES del sistema"""
    
    def __init__(self, symbol: str = 'BTC/USD:USD', capital: float = 1000.0, 
                 enable_slippage: bool = False):
        self.symbol = symbol
        self.capital = capital
        
        # Check if slippage should be enabled via feature flag
        if VALIDATION_AVAILABLE and is_feature_enabled(FeatureFlag.SLIPPAGE_SIM):
            enable_slippage = True
        
        # Componentes REALES
        self.market_data = SynchronizedMultiTimeframeBuffer(symbol)
        self.execution = MockExecution(enable_slippage=enable_slippage)
        self.external_data = MockExternalData()
        self.trade_logger = MockTradeLogger()
        
        if enable_slippage:
            logger.info("📉 Slippage simulation ENABLED (conservative backtest)")
        
        # Signal Engine REAL - with BACKTEST enforcement mode (relaxed thresholds)
        self.engine = SignalEngine(
            market_data=self.market_data,
            execution=self.execution,
            external_data=self.external_data,
            trade_logger=self.trade_logger,
            enforcement_mode='backtest'  # Relaxed edge thresholds for backtesting
        )
        self.engine.symbol = symbol
        self.engine.risk_engine.capital = capital
        
        # Stats
        self.equity_curve: List[float] = [capital]
        self.trades_closed: List[dict] = []
    
    async def run(self, df_1m: pd.DataFrame, df_5m: pd.DataFrame, df_1h: pd.DataFrame):
        """
        Ejecuta el backtest iterando por cada candle 1m
        """
        logger.info(f"🚀 Starting backtest with {len(df_1m)} 1m candles...")
        
        # Precalcular índices de 5m y 1h para cada timestamp 1m
        df_5m_indexed = df_5m.set_index('timestamp')
        df_1h_indexed = df_1h.set_index('timestamp')
        
        # Necesitamos warmup de datos
        # FIX (Jan 3, 2026): Ajustar warmup según datos disponibles
        # ADX necesita period*2 = 28 velas, EMA200 necesita 200 velas
        available_1h = len(df_1h)
        available_5m = len(df_5m)
        available_1m = len(df_1m)
        
        # Calcular warmup dinámico (dejar al menos 20% para backtest)
        warmup_1h = min(210, int(available_1h * 0.8))  # Para EMA 200
        warmup_5m = min(50, int(available_5m * 0.3))
        warmup_1m = min(50, int(available_1m * 0.1))
        
        logger.info(f"  Available data: 1h={available_1h}, 5m={available_5m}, 1m={available_1m}")
        logger.info(f"  Using warmup: 1h={warmup_1h}, 5m={warmup_5m}, 1m={warmup_1m}")
        
        if warmup_1h < 50:
            logger.warning(f"⚠️ Insufficient 1h data for reliable backtest (need 210+, have {available_1h})")
            logger.warning(f"   ADX and EMA200 may return NaN - recommend --days 14 or more")
        
        # Cargar warmup de 1h
        for i, row in df_1h.head(warmup_1h).iterrows():
            candle = self._row_to_candle(row, '1h')
            self.market_data.add_candle(candle)
        
        # Cargar warmup de 5m
        for i, row in df_5m.head(warmup_5m).iterrows():
            candle = self._row_to_candle(row, '5m')
            self.market_data.add_candle(candle)
        
        # Cargar warmup de 1m
        for i, row in df_1m.head(warmup_1m).iterrows():
            candle = self._row_to_candle(row, '1m')
            self.market_data.add_candle(candle)
        
        logger.info(f"  Warmup complete: 1h={len(self.market_data.candles['1h'])}, "
                   f"5m={len(self.market_data.candles['5m'])}, "
                   f"1m={len(self.market_data.candles['1m'])}")
        
        # Iterar por candles 1m (después del warmup)
        total = len(df_1m) - warmup_1m
        processed = 0
        
        # FIX (Jan 3, 2026): Track last added timestamps to avoid duplicates
        last_5m_ts = None
        last_1h_ts = None
        
        for idx in range(warmup_1m, len(df_1m)):
            row = df_1m.iloc[idx]
            ts = row['timestamp']
            
            # Agregar candle 1m
            candle_1m = self._row_to_candle(row, '1m')
            self.market_data.add_candle(candle_1m)
            
            # Actualizar 5m si hay nueva vela (solo cuando cambia el timestamp 5m)
            ts_5m = ts.floor('5min')
            if ts_5m != last_5m_ts and ts_5m in df_5m_indexed.index:
                last_5m_ts = ts_5m
                row_5m = df_5m_indexed.loc[ts_5m]
                if isinstance(row_5m, pd.DataFrame):
                    row_5m = row_5m.iloc[0]
                candle_5m = {
                    'timestamp': ts_5m,
                    'open': float(row_5m['open']),
                    'high': float(row_5m['high']),
                    'low': float(row_5m['low']),
                    'close': float(row_5m['close']),
                    'volume': float(row_5m['volume']),
                    'timeframe': '5m'
                }
                self.market_data.add_candle(candle_5m)
            
            # Actualizar 1h si hay nueva vela (solo cuando cambia el timestamp 1h)
            ts_1h = ts.floor('1h')
            if ts_1h != last_1h_ts and ts_1h in df_1h_indexed.index:
                last_1h_ts = ts_1h
                row_1h = df_1h_indexed.loc[ts_1h]
                if isinstance(row_1h, pd.DataFrame):
                    row_1h = row_1h.iloc[0]
                candle_1h = {
                    'timestamp': ts_1h,
                    'open': float(row_1h['open']),
                    'high': float(row_1h['high']),
                    'low': float(row_1h['low']),
                    'close': float(row_1h['close']),
                    'volume': float(row_1h['volume']),
                    'timeframe': '1h'
                }
                self.market_data.add_candle(candle_1h)
            
            # Actualizar precio en mock execution
            current_price = float(row['close'])
            self.execution.set_price(current_price)
            # FIX: Update last_price directly (market_data.get_current_price() uses this)
            # _current_price was wrong - the real code uses last_price
            self.market_data.last_price = current_price
            
            # ⭐ EJECUTAR TICK DEL SISTEMA REAL
            try:
                await self.engine._process_tick()
            except Exception as e:
                logger.debug(f"Tick error at {ts}: {e}")
            
            # Registrar equity
            equity = self._calculate_equity(current_price)
            self.equity_curve.append(equity)
            
            # Progress
            processed += 1
            if processed % 1000 == 0:
                logger.info(f"  Progress: {processed}/{total} ({100*processed/total:.1f}%)")
        
        # Cerrar posición abierta al final
        if self.engine.risk_engine.has_open_position():
            logger.info("  Closing open position at end of backtest...")
            await self.engine.emergency_close_all()
        
        return self._generate_results()
    
    def _row_to_candle(self, row: pd.Series, timeframe: str) -> dict:
        return {
            'timestamp': row['timestamp'],
            'open': float(row['open']),
            'high': float(row['high']),
            'low': float(row['low']),
            'close': float(row['close']),
            'volume': float(row['volume']),
            'timeframe': timeframe
        }
    
    def _calculate_equity(self, current_price: float) -> float:
        """Calculate current equity = initial capital + realized PnL + unrealized PnL"""
        # Start with initial capital + realized PnL from closed trades
        realized_pnl = self.engine.risk_engine.total_pnl
        equity = self.capital + realized_pnl
        
        # Add unrealized PnL from open position
        pos = self.engine.risk_engine.position
        if pos:
            if pos.direction == 'LONG':
                unrealized_pnl = (current_price - pos.entry_price) * pos.size
            else:
                unrealized_pnl = (pos.entry_price - current_price) * pos.size
            equity += unrealized_pnl
        return equity
    
    def _generate_results(self) -> dict:
        """Genera reporte de resultados"""
        trades = [e for e in self.trade_logger.events if e['type'] == 'trade_closed']
        
        total_trades = len(trades)
        wins = sum(1 for t in trades if t['data'].get('pnl', 0) > 0)
        losses = sum(1 for t in trades if t['data'].get('pnl', 0) < 0)
        
        # Use risk_engine.total_pnl as source of truth (includes partial closes)
        total_pnl = self.engine.risk_engine.total_pnl
        
        # Also calculate from trade logger for verification
        logged_pnl = sum(t['data'].get('pnl', 0) for t in trades)
        if abs(total_pnl - logged_pnl) > 1.0:
            logger.warning(f"⚠️ PnL mismatch: risk_engine={total_pnl:.2f}, trade_log={logged_pnl:.2f}")
        
        # Final equity = initial + total_pnl (should match equity_curve[-1])
        final_equity = self.capital + total_pnl
        
        # Drawdown
        equity = np.array(self.equity_curve)
        peak = np.maximum.accumulate(equity)
        drawdown = (peak - equity) / peak * 100
        max_dd = np.max(drawdown)
        
        return {
            'total_trades': total_trades,
            'wins': wins,
            'losses': losses,
            'win_rate': wins / total_trades * 100 if total_trades > 0 else 0,
            'total_pnl': total_pnl,
            'final_equity': final_equity,
            'return_pct': (final_equity - self.capital) / self.capital * 100,
            'max_drawdown_pct': max_dd,
            'trades': trades
        }


def print_results(results: dict):
    """Imprime resultados formateados"""
    trades = results.get('trades', [])
    
    # Calculate R metrics
    r_values = [t['data'].get('r', 0) for t in trades if 'r' in t['data']]
    mfe_values = [t['data'].get('mfe_r', 0) for t in trades if 'mfe_r' in t['data']]
    mae_values = [t['data'].get('mae_r', 0) for t in trades if 'mae_r' in t['data']]
    
    avg_r = sum(r_values) / len(r_values) if r_values else 0
    avg_mfe = sum(mfe_values) / len(mfe_values) if mfe_values else 0
    avg_mae = sum(mae_values) / len(mae_values) if mae_values else 0
    expectancy = avg_r if r_values else 0
    
    # Profit factor
    gross_profit = sum(t['data'].get('pnl', 0) for t in trades if t['data'].get('pnl', 0) > 0)
    gross_loss = abs(sum(t['data'].get('pnl', 0) for t in trades if t['data'].get('pnl', 0) < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    # Exit reason breakdown
    exit_reasons = {}
    for t in trades:
        reason = t['data'].get('reason', 'unknown')
        if reason not in exit_reasons:
            exit_reasons[reason] = {'count': 0, 'pnl': 0, 'r': []}
        exit_reasons[reason]['count'] += 1
        exit_reasons[reason]['pnl'] += t['data'].get('pnl', 0)
        if 'r' in t['data']:
            exit_reasons[reason]['r'].append(t['data']['r'])
    
    print("\n" + "="*70)
    print("📊 BACKTEST RESULTS (WITH R-METRICS)")
    print("="*70)
    print(f"Total Trades:    {results['total_trades']}")
    print(f"Wins:            {results['wins']}")
    print(f"Losses:          {results['losses']}")
    print(f"Win Rate:        {results['win_rate']:.2f}%")
    print(f"Profit Factor:   {profit_factor:.2f}")
    print(f"Total PnL:       ${results['total_pnl']:.2f}")
    print(f"Final Equity:    ${results['final_equity']:.2f}")
    print(f"Return:          {results['return_pct']:.2f}%")
    print(f"Max Drawdown:    {results['max_drawdown_pct']:.2f}%")
    print("-"*70)
    print(f"📈 R-METRICS:")
    print(f"  Expectancy:    {expectancy:.3f}R")
    print(f"  Avg R per trade: {avg_r:.3f}R")
    print(f"  Avg MFE:       {avg_mfe:.2f}R (max favorable move)")
    print(f"  Avg MAE:       {avg_mae:.2f}R (max adverse move)")
    print(f"  R tracked:     {len(r_values)}/{len(trades)} trades")
    print("-"*70)
    print("📊 EXIT REASON BREAKDOWN:")
    for reason, data in sorted(exit_reasons.items(), key=lambda x: x[1]['count'], reverse=True):
        avg_r_reason = sum(data['r']) / len(data['r']) if data['r'] else 0
        print(f"  {reason:20s}: {data['count']:3d} trades | PnL: ${data['pnl']:7.2f} | Avg R: {avg_r_reason:.2f}")
    print("="*70)
    
    if trades:
        print("\n📝 Trade Log (last 10):")
        for i, t in enumerate(trades[-10:], 1):
            d = t['data']
            r_val = d.get('r', 0)
            print(f"  {i}. {d.get('direction', '?'):5s} Entry: {d.get('entry', 0):.0f} → Exit: {d.get('exit', 0):.0f} | "
                  f"PnL: ${d.get('pnl', 0):6.2f} | R: {r_val:+.2f} | {d.get('reason', 'N/A')}")


async def main():
    parser = argparse.ArgumentParser(description='Backtest with Kraken data using REAL strategy classes')
    parser.add_argument('--symbol', default='BTC/USD:USD', help='Kraken Futures symbol')
    parser.add_argument('--days', type=int, default=7, help='Days of history')
    parser.add_argument('--offset', type=int, default=0, help='Offset days from today (0=recent, 7=week ago)')
    parser.add_argument('--capital', type=float, default=1000, help='Initial capital (EUR)')
    parser.add_argument('--slippage', action='store_true', help='Enable slippage simulation (conservative)')
    args = parser.parse_args()
    
    # Descargar datos
    df_1m, df_5m, df_1h = download_kraken_data(args.symbol, args.days, args.offset)
    
    if len(df_1m) < 1000:
        logger.error("Not enough data downloaded. Check symbol or connection.")
        return
    
    # Ejecutar backtest
    runner = BacktestRunner(
        symbol=args.symbol, 
        capital=args.capital,
        enable_slippage=args.slippage
    )
    results = await runner.run(df_1m, df_5m, df_1h)
    
    # Mostrar resultados
    print_results(results)


if __name__ == '__main__':
    asyncio.run(main())
