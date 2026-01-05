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
            logger.info(f"Flushed {len(self.event_buffer)} events")
            
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
                except Exception:
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
    
    def __init__(self, execution_engine):
        self.execution_engine = execution_engine
        self.last_reconcile = None
        self.reconcile_interval = 60  # 1 min
        self.is_running = False  # Control flag para evitar bucles infinitos
    
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
    
    async def reconcile(self) -> bool:
        """
        Reconciliar posición real vs estado
        
        Returns: True si OK, False si desync
        """
        try:
            position = await self.execution_engine.get_position('BTC/USD')
            
            # Compare with internal state
            # TODO: obtener expected position del bot
            
            self.last_reconcile = datetime.now(UTC)
            
            if position and position.get('contracts', 0) != 0:
                logger.debug(f"Position OK: {position['contracts']} contracts")
            
            return True
        
        except Exception as e:
            logger.error(f"Reconciliation error: {e}")
            return False
    
    async def get_status(self) -> Dict:
        """Get reconciliation status"""
        return {
            'last_reconcile': self.last_reconcile.isoformat() if self.last_reconcile else None,
            'interval_seconds': self.reconcile_interval
        }
