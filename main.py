# main.py

import asyncio
import logging
import os

# --- Importar todos los componentes del sistema ---
from ingest.kafka_producer import KafkaProducer
# Elige el ingestor que quieres usar. Podrías tener una lógica para cargar varios.
from ingest.ccxt_ingestor import CCXTIngestor 
# from ingest.polygon_ingestor import PolygonIngestor

from storage.writer_parquet import ParquetWriter
from engine.signal_engine import SignalEngine
from engine.risk_engine import RiskEngine
from output.output_manager import OutputManager
from monitoring.metrics import start_metrics_server

# --- Configuración de Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)-15s - %(levelname)-8s - %(message)s'
)
logger = logging.getLogger('MainApp')

# --- Configuración de la Aplicación (leída de variables de entorno) ---
KAFKA_SERVERS = os.getenv('KAFKA_BOOTSTRAP_SERVERS', 'localhost:9092')
RAW_DATA_TOPIC = 'raw_trades'
SYMBOLS = ['BTC/USDT', 'ETH/USDT'] # Símbolos para el CCXTIngestor
PARQUET_DIR = "./storage/writer_parquet"


async def main():
    """
    Punto de entrada principal que inicializa y ejecuta todos los servicios.
    """
    logger.info("🚀 Iniciando la aplicación de Daytrading...")
    
    # Lista para mantener todas las tareas en ejecución
    tasks = []
    
    try:
        # --- 1. Inicializar componentes base ---
        kafka_producer = KafkaProducer()
        await kafka_producer.initialize()
        
        output_manager = OutputManager()
        
        # --- 2. Inicializar los Módulos Principales ---
        
        # Ingestor de Datos (usando CCXT en este ejemplo)
        data_ingestor = CCXTIngestor(
            symbols=SYMBOLS,
            kafka_producer=kafka_producer
        )
        
        # Escritor de Parquet
        parquet_writer = ParquetWriter(
        topic="raw_trades",
        base_path=PARQUET_DIR,
        servers=KAFKA_SERVERS
        )

        await parquet_writer.start()


        
        # Motor de Riesgo
        risk_engine = RiskEngine(
            capital=10000.0, # Capital de ejemplo
            risk_per_trade_pct=0.01, # 1% de riesgo por operación
            output_manager=output_manager
        )
        
        # Motor de Señales
        signal_engine = SignalEngine(
            kafka_topic=RAW_DATA_TOPIC,
            kafka_servers=KAFKA_SERVERS,
            risk_engine=risk_engine
        )
        
        # --- 3. Iniciar todos los servicios como tareas asíncronas ---
        tasks.append(asyncio.create_task(data_ingestor.run(), name="Ingestor"))
        tasks.append(asyncio.create_task(parquet_writer.start(), name="ParquetWriter"))
        tasks.append(asyncio.create_task(signal_engine.start(), name="SignalEngine"))
        
        # Iniciar el servidor de métricas (no es una tarea asíncrona bloqueante)
        start_metrics_server()
        
        logger.info("✅ Todos los servicios han sido iniciados. La aplicación está en funcionamiento.")
        
        # Esperar a que todas las tareas finalicen (lo cual no ocurrirá a menos que fallen)
        await asyncio.gather(*tasks)

    except asyncio.CancelledError:
        logger.info("Recibida señal de cancelación. La aplicación se está apagando...")
    except Exception as e:
        logger.critical(f"Error crítico en la aplicación principal: {e}", exc_info=True)
    finally:
        # --- 4. Lógica de apagado limpio ---
        logger.info("Iniciando secuencia de apagado limpio...")
        
        # Cancelar todas las tareas en ejecución
        for task in tasks:
            task.cancel()
        
        # Esperar a que todas las tareas se cancelen
        await asyncio.gather(*tasks, return_exceptions=True)
        
        # Detener componentes que requieren un cierre explícito
        if 'kafka_producer' in locals() and kafka_producer._producer:
            await kafka_producer.stop()
            
        logger.info("👋 Aplicación apagada correctamente.")


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        # Iniciar el bucle de eventos principal
        main_task = loop.create_task(main())
        loop.run_until_complete(main_task)
    except KeyboardInterrupt:
        logger.info("Detectado KeyboardInterrupt (Ctrl+C). Apagando...")
        # Cancelar la tarea principal para iniciar el apagado limpio
        main_task.cancel()
        # Esperar a que la lógica de 'finally' en main() se complete
        loop.run_until_complete(main_task)
    finally:
        loop.close()
