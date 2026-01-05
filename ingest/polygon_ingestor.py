import asyncio
import json
import logging
import os
import websockets
from .base_ingestor import BaseIngestor
from ingest.kafka_producer import KafkaProducer

logger = logging.getLogger(__name__)


API_KEY = os.getenv("POLYGON_API_KEY")
WEBSOCKET_URL = f"wss://socket.polygon.io/stocks?apiKey={API_KEY}"
SYBOLS = ["T.SPY", "T.QQQ"]  # Example symbols to subscribe to

class PolygonIngestor(BaseIngestor):

    def __init__(self, uri: str, api_key: str, symbols: list[str], kafka_producer: KafkaProducer):
        self.uri = uri
        self.api_key = api_key
        self.symbols = symbols
        self.kafka_producer = kafka_producer
        self.connection = None
        self.running = False

    async def _connect(self):
        logger.info("Connecting to Polygon WebSocket...")
        self.connection = await websockets.connect(self.uri)

        #Polygon requiere que envíes tu API Key al conectarte al websocket.
        await self.connection.send(json.dumps({
            "action": "auth",
            "params": self.api_key
        }))
        auth_response = await self.connection.recv()
        logger.info(f"Auth response: {auth_response}")

        #decirle al servidor qué símbolos quieres escuchar
        subscription_params = ",".join(self.symbols)
        await self.connection.send(json.dumps({
            "action": "subscribe",
            "params": subscription_params
        }))
        logger.info(f"Subscribed to symbols: {subscription_params}")
    
    async def _listener(self):
        while self.running:
            try:
                # Timeout para permitir verificar self.running
                raw_message = await asyncio.wait_for(
                    self.connection.recv(),
                    timeout=30.0
                )
                messages = json.loads(raw_message)
                
                # Asegurar que messages es una lista
                if not isinstance(messages, list):
                    messages = [messages]
                    
                for message in messages:
                    if not self.running:
                        break
                    
                    if message.get("ev") == "T":  # Trade event
                        symbol = message.get("sym")
                        price = message.get("p")
                        volume = message.get("s")
                        timestamp = message.get("t")

                        trade_data = {
                            "symbol": symbol,
                            "price": price,
                            "volume": volume,
                            "timestamp": timestamp
                        }

                        await self.kafka_producer.send_message("raw_trades", trade_data)
            except asyncio.TimeoutError:
                # Timeout normal, continuar para verificar running
                continue
            except asyncio.CancelledError:
                logger.info("Listener cancelled")
                break
            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket connection closed in listener")
                break
            except Exception as e:
                logger.error(f"Error in listener: {e}")
                if self.running:
                    await asyncio.sleep(1.0)

    async def run(self):
        # Implementation for polygon ingestion
        self.running = True
        reconnect_delay = 1

        while self.running:
            try:
                await self._connect()
                await self._listener()
            except Exception as e:
                logger.error(f"Error in PolygonIngestor: {e}")
                logger.info(f"Reconnecting in {reconnect_delay} seconds...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)  # Exponential backoff up to 60 seconds
            else:
                reconnect_delay = 1  # Reset delay on successful connection
        pass

    async def close(self):
        """Cierre asíncrono con limpieza de recursos"""
        logger.info("Closing PolygonIngestor...")
        self.running = False
        
        if self.connection:
            try:
                await asyncio.wait_for(self.connection.close(), timeout=5.0)
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"Error closing websocket: {e}")
            self.connection = None
        
        logger.info("PolygonIngestor closed")

    def stop(self):
        """Detención síncrona (solo marca flag)"""
        self.running = False

async def main():
    kafka_producer = KafkaProducer()
    await kafka_producer.initialize()

    polygon_ingestor = PolygonIngestor(
        uri=WEBSOCKET_URL,
        api_key=API_KEY,
        symbols=SYBOLS,
        kafka_producer=kafka_producer
    )

    try:
        await polygon_ingestor.run()
    finally:
        await polygon_ingestor.close()
        await kafka_producer.close()
    
if __name__ == "__main__":
    # Para ejecutar este servicio:
    # 1. Asegúrate de que Kafka esté corriendo.
    # 2. Exporta tu clave de API: export POLYGON_API_KEY="tu_clave"
    # 3. Ejecuta: python -m ingest.ws_client
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Polygon Ingestor stopped manually.")
