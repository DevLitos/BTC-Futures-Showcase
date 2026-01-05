import logging
import pandas as pd
import asyncio
import time
import json
from aiokafka import AIOKafkaConsumer

logger = logging.getLogger(__name__)


class ParquetWriter:
    def __init__(self, topic: str, base_path: str, servers: str):
        """
        Ahora ParquetWriter crea su propio KafkaConsumer
        para alinearse con tu main.py original.
        """
        self.topic = topic
        self.base_path = base_path
        self.kafka_servers = servers

        self.consumer = None
        self.buffer = []
        self.batch_size = 1000
        self.timeout_seconds = 15
        self.last_flush_time = time.time()
        
        # Control de flujo
        self.is_running = False
        self._consume_task = None
        self._timeout_task = None

    async def start(self):
        logger.info(f"📁 Iniciando ParquetWriter escuchando el topic '{self.topic}'...")

        # Crear consumer internamente
        self.consumer = AIOKafkaConsumer(
            self.topic,
            bootstrap_servers=self.kafka_servers,
            enable_auto_commit=True,
            auto_offset_reset="latest",
            value_deserializer=lambda x: x.decode("utf-8")
        )
        await self.consumer.start()
        
        self.is_running = True
        self._consume_task = asyncio.create_task(self._consume_loop())
        self._timeout_task = asyncio.create_task(self._timeout_loop())

    async def stop(self):
        logger.info("🛑 ParquetWriter apagándose...")
        self.is_running = False
        
        # Cancelar tasks de background
        for task in [self._consume_task, self._timeout_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
        
        # Flush buffer restante antes de cerrar
        await self._flush_buffer()
        
        if self.consumer:
            await self.consumer.stop()
        logger.info("ParquetWriter apagado correctamente.")

    async def _consume_loop(self):
        try:
            async for msg in self.consumer:
                if not self.is_running:
                    break
                try:
                    data = json.loads(msg.value)
                    self.buffer.append(data)

                    if len(self.buffer) >= self.batch_size:
                        await self._flush_buffer()

                except Exception as e:
                    logger.error(f"Error procesando mensaje en ParquetWriter: {e}")
        except asyncio.CancelledError:
            logger.info("Consume loop cancelled")

    async def _timeout_loop(self):
        try:
            while self.is_running:
                await asyncio.sleep(1)
                if time.time() - self.last_flush_time >= self.timeout_seconds:
                    logger.info("⏱ Timeout alcanzado → escribiendo buffer...")
                    await self._flush_buffer()
        except asyncio.CancelledError:
            logger.info("Timeout loop cancelled")

    async def _flush_buffer(self):
        if not self.buffer:
            return

        data = self.buffer.copy()
        self.buffer.clear()
        self.last_flush_time = time.time()

        logger.info(f"📦 Escribiendo lote de {len(data)} mensajes en Parquet...")

        df = pd.DataFrame(data)

        # ---- Normalizar timestamp_ns ----
        if "timestamp_ns" not in df.columns:
            logger.warning("Mensajes sin 'timestamp_ns'. Intentando inferir...")

            if "timestamp_ms" in df.columns:
                # Convert milliseconds to nanoseconds using pandas numeric operations
                df["timestamp_ns"] = pd.to_numeric(df["timestamp_ms"], errors='coerce') * 1_000_000
                df["timestamp_ns"] = df["timestamp_ns"].astype('uint64')

            elif "timestamp" in df.columns:
                def infer_ns(x):
                    try:
                        v = float(x)  # Use float first to handle large numbers
                    except (ValueError, TypeError):
                        return None
                    if v > 1e18:  # ns
                        return int(v)
                    if v > 1e12:  # ms
                        return int(v * 1_000_000)
                    if v > 1e9:   # s
                        return int(v * 1_000_000_000)
                    return int(v * 1_000_000_000)

                # Use uint64 to handle large nanosecond timestamps
                df["timestamp_ns"] = pd.to_numeric(df["timestamp"].apply(infer_ns), errors='coerce').astype('uint64')

            # fallback si no se pudo inferir
            if "timestamp_ns" not in df.columns or df["timestamp_ns"].isnull().all():
                now_ns = int(time.time() * 1_000_000_000)
                df["timestamp_ns"] = now_ns
                logger.warning("⚠ No se pudo inferir timestamp → usando timestamp actual.")
        else:
            # Ensure timestamp_ns is uint64 even if it exists
            df["timestamp_ns"] = pd.to_numeric(df["timestamp_ns"], errors='coerce').astype('uint64')

        # Datetime
        try:
            # Handle both int64 and uint64 timestamps
            df["timestamp"] = pd.to_datetime(df["timestamp_ns"], unit="ns", errors='coerce')
            # Fill any NaT values with current time
            if df["timestamp"].isnull().any():
                df["timestamp"].fillna(pd.to_datetime(int(time.time() * 1e9), unit="ns"), inplace=True)
        except Exception as e:
            logger.error(f"Error convirtiendo timestamp: {e}")
            df["timestamp"] = pd.to_datetime(int(time.time() * 1e9), unit="ns")

        # Particiones
        df["year"] = df["timestamp"].dt.year
        df["month"] = df["timestamp"].dt.month
        df["day"] = df["timestamp"].dt.day

        # Escribir parquet
        try:
            df.to_parquet(
                self.base_path,
                engine="pyarrow",
                partition_cols=["symbol", "year", "month", "day"],
                existing_data_behavior="overwrite_or_ignore"
            )
            logger.info("✅ Lote escrito correctamente.")
        except Exception as e:
            logger.error(f"❌ Error escribiendo Parquet: {e}")
