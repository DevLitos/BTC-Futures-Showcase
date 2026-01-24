import asyncio
import ccxt.pro as ccxtpro
import logging
from . import base_ingestor as baseIngestor
from . import kafka_producer as kafkaProducer


logger = logging.getLogger(__name__)

class CCXTIngestor(baseIngestor.BaseIngestor):

    def __init__(self, symbols: list, kafka_producer: kafkaProducer):  # pyright: ignore[reportGeneralTypeIssues]
        self.symbols = symbols
        self.kafka_producer = kafka_producer
        self.exchange_class = ccxtpro.binance  # Clase, no instancia
        self.running = False
        self._tasks = []  # Lista de tasks para cancelación
        self._exchanges = []  # Lista de instancias de exchange para cierre
    
    async def _trade_loop(self, symbol: str):
        # Instanciar el exchange
        exchange = self.exchange_class()
        self._exchanges.append(exchange)

        try:
            await exchange.load_markets()

            while self.running:
                try:
                    # Obtener trades con timeout para permitir cancelación
                    trades = await asyncio.wait_for(
                        exchange.watch_trades(symbol),
                        timeout=30.0
                    )

                    for trade in trades:
                        if not self.running:
                            break
                        # CCXT provides timestamp in milliseconds, convert to nanoseconds
                        timestamp_ms = int(trade['timestamp'])  # pyright: ignore[reportArgumentType]
                        trade_data = {
                            "symbol": symbol,
                            "price": trade['price'],
                            "amount": trade['amount'],
                            "timestamp_ms": timestamp_ms,
                            "timestamp_ns": timestamp_ms * 1_000_000
                        }

                        await self.kafka_producer.send_message("raw_trades", trade_data)  # pyright: ignore[reportAttributeAccessIssue]

                except asyncio.TimeoutError:
                    # Timeout es normal, continuar el loop
                    continue
                except asyncio.CancelledError:
                    logger.info(f"Trade loop for {symbol} cancelled")
                    break
                except Exception as e:
                    logger.error(f"Error fetching trades for {symbol}: {e}")
                    if self.running:
                        await asyncio.sleep(4)
        finally:
            try:
                await asyncio.wait_for(exchange.close(), timeout=5.0)
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"Error closing exchange for {symbol}: {e}")

    
    async def run(self):
        self.running = True
        self._tasks = []
        self._exchanges = []
        
        # Crear tasks para cada símbolo
        for symbol in self.symbols:
            task = asyncio.create_task(self._trade_loop(symbol))
            self._tasks.append(task)
        
        # Esperar a que todas las tasks terminen
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            logger.info("CCXTIngestor run() cancelled")

    async def close(self):
        """Cierre asíncrono con cancelación de tasks"""
        logger.info("Closing CCXTIngestor...")
        self.running = False
        
        # Cancelar todas las tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
        
        # Esperar a que las tasks terminen
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        
        # Cerrar exchanges que aún estén abiertos
        for exchange in self._exchanges:
            try:
                if hasattr(exchange, 'close'):
                    await asyncio.wait_for(exchange.close(), timeout=5.0)
            except Exception as e:
                logger.warning(f"Error closing exchange: {e}")
        
        self._tasks = []
        self._exchanges = []
        logger.info("CCXTIngestor closed")

    def stop(self):
        """Detención síncrona (solo marca flag)"""
        self.running = False
