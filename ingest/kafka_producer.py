import asyncio
import json
import logging
import os
from aiokafka import AIOKafkaProducer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP_SERVERS = os.getenv('KAFKA_BOOTSTRAP_SERVERS', 'localhost:9092')


class KafkaProducer:
    _instance = None
    _producer = None
    _loop = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(KafkaProducer, cls).__new__(cls)
        return cls._instance


def get_kafka_producer(instance=None):
    """
    Get KafkaProducer instance. Supports DI for testing.
    
    Args:
        instance: Optional explicit instance (for dependency injection)
    
    Returns:
        KafkaProducer instance
    """
    if instance is not None:
        return instance
    return KafkaProducer()


def reset_kafka_producer():
    """Reset singleton for testing. Use with caution."""
    KafkaProducer._instance = None

    async def initialize(self):
        if self._producer is None:
            self._loop = asyncio.get_running_loop()
            logger.info(f"Inicializando productor de Kafka en {KAFKA_BOOTSTRAP_SERVERS}...")

            try:
                self._producer = AIOKafkaProducer(
                    loop=self._loop,
                    bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
                    value_serializer=lambda v: json.dumps(v).encode('utf-8'),
                    acks='all'
                )
                await self._producer.start()
                logger.info("Productor de Kafka inicializado y conectado correctamente.")

            except Exception as e:
                logger.error(f"Error fatal al inicializar el productor de Kafka: {e}")
                self._producer = None
                raise

    async def send_message(self, topic: str, message: dict):
        if self._producer is None:
            logger.error("Productor de Kafka no inicializado. Llama a 'initialize()' primero.")
            return

        try:
            await self._producer.send_and_wait(topic, message)
            logger.debug(f"Mensaje enviado al tópico '{topic}': {message}")
        except Exception as e:
            logger.error(f"Error al enviar mensaje al tópico '{topic}': {e}")

    async def stop(self):
        if self._producer:
            logger.info("Cerrando productor de Kafka...")
            await self._producer.stop()
            self._producer = None
            logger.info("Productor de Kafka cerrado correctamente.")
