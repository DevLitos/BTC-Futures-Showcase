import asyncio
import ccxt.pro as ccxtpro
import logging
from . import base_ingestor as baseIngestor
from . import kafka_producer as kafkaProducer


logging = logging.getLogger(__name__)

class CCXTIngestor(baseIngestor.BaseIngestor):

    def __init__(self, symbols: list, kafka_producer: kafkaProducer):
        self.symbols = symbols
        self.kafka_producer = kafka_producer
        self.exchange = ccxtpro.binance
        self.running = False
    
    async def _trade_loop(self, symbol: str):
        # Instanciar el exchange
        exchange = self.exchange()

        await exchange.load_markets()

        while self.running:
            try:
                # Obtener trades
                trades = await exchange.watch_trades(symbol)

                for trade in trades:
                    # CCXT provides timestamp in milliseconds, convert to nanoseconds
                    timestamp_ms = int(trade['timestamp'])
                    trade_data = {
                        "symbol": symbol,
                        "price": trade['price'],
                        "amount": trade['amount'],
                        "timestamp_ms": timestamp_ms,
                        "timestamp_ns": timestamp_ms * 1_000_000
                    }

                    await self.kafka_producer.send_message("raw_trades", trade_data)

            except Exception as e:
                logging.error(f"Error fetching trades for {symbol}: {e}")
                await asyncio.sleep(4)

        await exchange.close()

    
    async def run(self):
        self.running = True
        # create a task for each symbol
        tasks = [self._trade_loop(symbol) for symbol in self.symbols]
        await asyncio.gather(*tasks)
        await self.exchange.close()


    def close(self):
        self.running = False
