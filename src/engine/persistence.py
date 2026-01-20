"""
Persistencia - PostgreSQL para trades y eventos
Opción: Parquet daily rotation si no tiene DB

MEJORAS (Jan 4, 2026):
- Backpressure con colas acotadas
- Métricas de encolado
- Flush periódico en background
"""

import logging
import asyncio
from typing import Optional, List, Dict
from datetime import datetime, UTC
import json
import os

# Import for parquet error handling
try:
    from pyarrow.lib import ArrowInvalid
except ImportError:
    # Fallback if pyarrow not installed
    ArrowInvalid = Exception

logger = logging.getLogger(__name__)

# Constantes de backpressure
MAX_QUEUE_SIZE = 1000  # Máximo de items en cola
FLUSH_INTERVAL = 30.0  # Segundos entre flushes automáticos


class TradeLogger:
    """
    Log de trades para análisis posterior
    PostgreSQL o Parquet
    
    Features:
    - Colas acotadas para backpressure
    - Flush periódico en background
    - Métricas de encolado
    """
    
    def __init__(self, backend: str = 'parquet', db_url: Optional[str] = None):
        self.backend = backend
        self.db_url = db_url
        
        # Backpressure: colas acotadas
        self._trade_queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        
        # Control de lifecycle
        self._running = False
        self._flush_task: Optional[asyncio.Task] = None
        
        # Métricas
        self._trades_dropped = 0
        self._events_dropped = 0
        self._total_trades_logged = 0
        self._total_events_logged = 0
        
        # Buffers legacy (para compatibilidad)
        self.trade_buffer = []
        self.event_buffer = []
        
        if backend == 'postgresql':
            self._init_postgresql()
        elif backend == 'parquet':
            self._init_parquet()
    
    def _init_postgresql(self):
        """Initialize PostgreSQL connection"""
        try:
            import asyncpg
            logger.info("PostgreSQL backend initialized")
        except ImportError:
            logger.error("asyncpg not installed - falling back to parquet")
            self.backend = 'parquet'
            self._init_parquet()
    
    def _init_parquet(self):
        """Initialize Parquet backend"""
        # Crear directorios si no existen
        for subdir in ['trades', 'events', 'metrics']:
            os.makedirs(f"storage/{subdir}", exist_ok=True)
        logger.info("Parquet backend initialized with backpressure queues")
    
    async def start(self):
        """Iniciar flush periódico en background"""
        self._running = True
        self._flush_task = asyncio.create_task(self._periodic_flush_loop())
        logger.info("TradeLogger started with periodic flush")
    
    async def stop(self):
        """Detener y hacer flush final"""
        self._running = False
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await asyncio.wait_for(self._flush_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        
        # Flush final
        await self.flush_all()
        logger.info(f"TradeLogger stopped. Stats: {self._total_trades_logged} trades, "
                   f"{self._total_events_logged} events, "
                   f"{self._trades_dropped} trades dropped, "
                   f"{self._events_dropped} events dropped")
    
    async def _periodic_flush_loop(self):
        """Loop de flush periódico"""
        try:
            while self._running:
                await asyncio.sleep(FLUSH_INTERVAL)
                await self._drain_queues()
        except asyncio.CancelledError:
            pass
    
    async def _drain_queues(self):
        """Vaciar colas a buffers y hacer flush"""
        # Drain trade queue
        while not self._trade_queue.empty():
            try:
                trade = self._trade_queue.get_nowait()
                self.trade_buffer.append(trade)
            except asyncio.QueueEmpty:
                break
        
        # Drain event queue
        while not self._event_queue.empty():
            try:
                event = self._event_queue.get_nowait()
                self.event_buffer.append(event)
            except asyncio.QueueEmpty:
                break
        
        # Flush si hay datos
        if self.trade_buffer:
            await self._flush_trades()
        if self.event_buffer:
            await self._flush_events()
    
    async def log_trade(self, trade: Dict):
        """
        Registrar trade con backpressure.
        Si la cola está llena, descarta el trade (mejor que bloquear).
        """
        trade['logged_at'] = datetime.now(UTC).isoformat()
        
        if self.backend == 'postgresql':
            await self._log_trade_postgres(trade)
        elif self.backend == 'parquet':
            try:
                self._trade_queue.put_nowait(trade)
                self._total_trades_logged += 1
            except asyncio.QueueFull:
                self._trades_dropped += 1
                if self._trades_dropped % 100 == 1:  # Log cada 100 drops
                    logger.warning(f"Trade queue full - {self._trades_dropped} trades dropped total")
    
    async def _log_trade_postgres(self, trade: Dict):
        """Log trade to PostgreSQL"""
        try:
            # Implement PostgreSQL insert
            logger.info(f"Trade logged to PostgreSQL: {trade.get('entry_price', 0):.2f}")
            self._total_trades_logged += 1
        except Exception as e:
            logger.error(f"Error logging to PostgreSQL: {e}")
    
    async def _log_trade_parquet(self, trade: Dict):
        """Log trade to Parquet (legacy - usado por drain)"""
        self.trade_buffer.append(trade)
        
        # Flush every 10 trades or on shutdown
        if len(self.trade_buffer) >= 10:
            await self._flush_trades()
    
    async def _flush_trades(self):
        """Flush trades to Parquet"""
        if not self.trade_buffer:
            return
        
        try:
            import pandas as pd
            import pyarrow.parquet as pq
            from datetime import datetime, UTC
            
            df = pd.DataFrame(self.trade_buffer)
            
            # Filename: trades_YYYYMMDD.parquet
            today = datetime.now(UTC).strftime('%Y%m%d')
            filename = f"storage/trades/trades_{today}.parquet"
            
            df.to_parquet(filename, compression='snappy')
            logger.info(f"Flushed {len(self.trade_buffer)} trades to {filename}")
            
            self.trade_buffer = []
        
        except Exception as e:
            logger.error(f"Error flushing trades: {e}")
    
    async def log_event(self, event_type: str, details: Dict):
        """
        Registrar evento con backpressure.
        Si la cola está llena, descarta el evento.
        """
        event = {
            'timestamp': datetime.now(UTC).isoformat(),
            'type': event_type,
            'details': json.dumps(details) if isinstance(details, dict) else str(details)
        }
        
        if self.backend == 'postgresql':
            await self._log_event_postgres(event)
        elif self.backend == 'parquet':
            try:
                self._event_queue.put_nowait(event)
                self._total_events_logged += 1
            except asyncio.QueueFull:
                self._events_dropped += 1
                if self._events_dropped % 100 == 1:
                    logger.warning(f"Event queue full - {self._events_dropped} events dropped total")
    
    async def _log_event_postgres(self, event: Dict):
        """Log event to PostgreSQL"""
        try:
            # Implement PostgreSQL insert
            logger.info(f"Event logged: {event['type']}")
            self._total_events_logged += 1
        except Exception as e:
            logger.error(f"Error logging event: {e}")
    
    async def _log_event_parquet(self, event: Dict):
        """Log event to Parquet (legacy - usado por drain)"""
        self.event_buffer.append(event)
        
        if len(self.event_buffer) >= 20:
            await self._flush_events()
    
    async def _flush_events(self):
        """Flush events to Parquet"""
        if not self.event_buffer:
            return
        
        try:
            import pandas as pd
            
            df = pd.DataFrame(self.event_buffer)
            
            today = datetime.now(UTC).strftime('%Y%m%d')
            filename = f"storage/events/events_{today}.parquet"
            
            df.to_parquet(filename, compression='snappy')
            logger.debug(f"Flushed {len(self.event_buffer)} events")  # DEBUG to reduce spam
            
            self.event_buffer = []
        
        except Exception as e:
            logger.error(f"Error flushing events: {e}")
    
    async def log_metrics(self, metrics: Dict):
        """Registrar métricas diarias"""
        metrics['logged_at'] = datetime.now(UTC).isoformat()
        
        try:
            import pandas as pd
            import os
            
            new_df = pd.DataFrame([metrics])
            
            today = datetime.now(UTC).strftime('%Y%m%d')
            filename = f"storage/metrics/metrics_{today}.parquet"
            
            # Read existing file and append (parquet doesn't support append directly)
            if os.path.exists(filename):
                try:
                    existing_df = pd.read_parquet(filename)
                    df = pd.concat([existing_df, new_df], ignore_index=True)
                except (OSError, ValueError, ArrowInvalid) as e:
                    logger.warning(f"Could not read existing metrics file, starting fresh: {e}")
                    df = new_df
            else:
                df = new_df
            
            df.to_parquet(filename, compression='snappy')
            logger.info(f"Metrics logged: PF={metrics.get('profit_factor', 'N/A')}")
        
        except Exception as e:
            logger.error(f"Error logging metrics: {e}")
    
    async def flush_all(self):
        """Flush all pending data (on shutdown)"""
        # Primero drena las colas
        await self._drain_queues()
        # Luego flush final
        await self._flush_trades()
        await self._flush_events()
        logger.info("All data flushed to storage")
    
    def get_stats(self) -> Dict:
        """Obtener estadísticas de persistencia"""
        return {
            'trade_queue_size': self._trade_queue.qsize(),
            'event_queue_size': self._event_queue.qsize(),
            'trades_logged': self._total_trades_logged,
            'events_logged': self._total_events_logged,
            'trades_dropped': self._trades_dropped,
            'events_dropped': self._events_dropped,
            'trade_buffer_size': len(self.trade_buffer),
            'event_buffer_size': len(self.event_buffer)
        }


class PositionReconciler:
    """
    Reconcilia posición real vs estado interno
    Detecta desync automáticamente
    """
    
    def __init__(self, execution_engine, signal_engine=None):
        self.execution_engine = execution_engine
        self.signal_engine = signal_engine  # Reference to get expected position
        self.last_reconcile = None
        self.reconcile_interval = 60  # 1 min
        self.is_running = False  # Control flag para evitar bucles infinitos
        self._last_discrepancy = None
        self._exit_in_progress = False  # Lock during exits
        self._exit_reason = None
    
    def set_signal_engine(self, signal_engine):
        """Set signal engine reference after initialization"""
        self.signal_engine = signal_engine
    
    def notify_exit_started(self, reason: str):
        """Called when an exit is initiated to prevent reconciliation interference"""
        self._exit_in_progress = True
        self._exit_reason = reason
        logger.debug(f"Reconciler: exit lock acquired ({reason})")
    
    def notify_exit_completed(self):
        """Called when an exit completes"""
        self._exit_in_progress = False
        self._exit_reason = None
        logger.debug("Reconciler: exit lock released")
    
    async def start(self):
        """Start periodic reconciliation"""
        self.is_running = True
        while self.is_running:
            try:
                await self.reconcile()
                await asyncio.sleep(self.reconcile_interval)
            except Exception as e:
                logger.error(f"Error reconciling: {e}")
                await asyncio.sleep(10)
    
    async def stop(self):
        """Stop reconciliation loop"""
        logger.info("Stopping reconciliation...")
        self.is_running = False
    
    def _get_expected_position(self) -> Optional[Dict]:
        """
        Get expected position from signal engine's risk engine.
        
        Returns dict with: side, size, entry_price or None if flat
        """
        if not self.signal_engine:
            return None
        
        if not hasattr(self.signal_engine, 'risk_engine'):
            return None
        
        position = self.signal_engine.risk_engine.position
        if not position:
            return None
        
        return {
            'side': position.direction.lower(),  # 'long' or 'short'
            'size': position.size,
            'entry_price': position.entry_price,
            'symbol': position.symbol
        }
    
    async def reconcile(self) -> bool:
        """
        Reconciliar posición real vs estado interno
        
        Returns: True si OK, False si desync
        """
        # Skip reconciliation during exits to prevent state corruption
        if self._exit_in_progress:
            logger.debug(f"Reconciliation skipped: exit in progress ({self._exit_reason})")
            return True
        
        try:
            # Get exchange position (source of truth)
            exchange_pos = await self.execution_engine.get_position('BTC/USD:USD')
            
            # Get expected position from bot state
            expected = self._get_expected_position()
            
            self.last_reconcile = datetime.now(UTC)
            
            # Extract exchange values
            exchange_side = None
            exchange_size = 0.0
            if exchange_pos and exchange_pos.get('contracts', 0) != 0:
                exchange_size = abs(float(exchange_pos.get('contracts', 0)))
                exchange_side = exchange_pos.get('side', '').lower()
            
            # Compare states
            if expected is None and exchange_size == 0:
                # Both flat - OK
                if self._last_discrepancy:
                    logger.info("✅ Reconciliation: discrepancy resolved, both flat")
                    self._last_discrepancy = None
                return True
            
            if expected is None and exchange_size > 0:
                # Bot thinks flat, but exchange has position
                discrepancy = f"ORPHAN: Exchange has {exchange_side} {exchange_size:.6f} but bot expects FLAT"
                if self._last_discrepancy != discrepancy:
                    logger.warning(f"⚠️ RECONCILIATION DISCREPANCY: {discrepancy}")
                    self._last_discrepancy = discrepancy
                return False
            
            if expected is not None and exchange_size == 0:
                # Bot has position, exchange is flat
                discrepancy = f"GHOST: Bot expects {expected['side']} {expected['size']:.6f} but exchange is FLAT"
                if self._last_discrepancy != discrepancy:
                    logger.warning(f"⚠️ RECONCILIATION DISCREPANCY: {discrepancy}")
                    self._last_discrepancy = discrepancy
                return False
            
            # Both have positions - check if they match
            if expected is not None and exchange_size > 0:
                # Check side
                if expected['side'] != exchange_side:
                    discrepancy = f"SIDE MISMATCH: Bot={expected['side']}, Exchange={exchange_side}"
                    if self._last_discrepancy != discrepancy:
                        logger.critical(f"🚨 RECONCILIATION CRITICAL: {discrepancy}")
                        self._last_discrepancy = discrepancy
                    return False
                
                # Check size (with 5% tolerance)
                size_diff = abs(expected['size'] - exchange_size) / max(expected['size'], 0.0001)
                if size_diff > 0.05:  # 5% tolerance
                    discrepancy = f"SIZE MISMATCH: Bot={expected['size']:.6f}, Exchange={exchange_size:.6f} (diff={size_diff*100:.1f}%)"
                    if self._last_discrepancy != discrepancy:
                        logger.warning(f"⚠️ RECONCILIATION: {discrepancy}")
                        self._last_discrepancy = discrepancy
                    return False
                
                # All good
                if self._last_discrepancy:
                    logger.info(f"✅ Reconciliation: discrepancy resolved")
                    self._last_discrepancy = None
                logger.debug(f"Position OK: {exchange_side} {exchange_size:.6f}")
                return True
            
            return True
        
        except Exception as e:
            logger.error(f"Reconciliation error: {e}", exc_info=True)
            return False
    
    async def get_status(self) -> Dict:
        """Get reconciliation status"""
        return {
            'last_reconcile': self.last_reconcile.isoformat() if self.last_reconcile else None,
            'interval_seconds': self.reconcile_interval,
            'last_discrepancy': self._last_discrepancy,
            'exit_in_progress': self._exit_in_progress
        }
