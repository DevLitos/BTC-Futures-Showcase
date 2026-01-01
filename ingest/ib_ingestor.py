import asyncio
from ib_insync import IB, util, Stock
import logging
import .base_ingestor as baseIngestor
import .kafka_producer as kafkaProducer

logging = logging.getLogger(__name__)

class IBIngestor(baseIngestor.BaseIngestor):

    def __init__(self, symbols: list[str], kafka_producer: kafkaProducer, 
    ib_host: str, ib_port: int, client_id: int = 1):
        self.symbols = symbols
        self.kafka_producer = kafka_producer
        self.ib = IB()
        self.ib_host = ib_host
        self.ib_port = ib_port
        self.client_id = client_id
    
    def _on_pending_tickers(self, tickers):
        last_trade = ticker.last_trade
        last_size = ticker.last_size
        timestamp_ns = util.time_ns()

        if last_trade & last_size:
            trade_data = {
                "symbol": ticker.contract.symbol,
                "last_trade": last_trade,
                "last_size": last_size,
                "timestamp_ns": timestamp_ns
            }
            asyncio.create_task(self.kafka_producer.send_message("raw_trades", trade_data))

    async def run(self):
        try:
            await self.ib.connectAsync(self.ib_host, self.ib_port, clientId=self.client_id)
            logger.info("Connected to IB Gateway/TWS")

            contracts = [Stock(symbol, 'SMART', 'USD') for symbol in self.symbols]
            for contract in contracts:
                self.ib.reqMktData(contract, '', False, False)
            
            self.ib.pendingTickersEvent += self._on_pending_tickers
            while self.ib.isConnected():
                await asyncio.sleep(1)
            
        except Exception as e:
            logging.error(f"Error in IBIngestor: {e}")
        finally:
            if self.ib.isConnected():
                self.ib.disconnect()
                logger.info("Disconnected from IB Gateway/TWS")
    
    def close(self):
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IB Gateway/TWS")
