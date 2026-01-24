import asyncio
from ib_insync import IB, util, Stock
import logging
from . import base_ingestor as baseIngestor
from . import kafka_producer as kafkaProducer

logger = logging.getLogger(__name__)

class IBIngestor(baseIngestor.BaseIngestor):

    def __init__(self, symbols: list[str], kafka_producer: kafkaProducer,  # pyright: ignore[reportGeneralTypeIssues] 
    ib_host: str, ib_port: int, client_id: int = 1):
        self.symbols = symbols
        self.kafka_producer = kafka_producer
        self.ib = IB()
        self.ib_host = ib_host
        self.ib_port = ib_port
        self.client_id = client_id
        self._pending_tasks = []  # Track fire-and-forget tasks
    
    async def _on_pending_tickers(self, tickers):
        """Process ticker updates and send to Kafka with proper task tracking"""
        for ticker in tickers:
            last_trade = ticker.last_trade
            last_size = ticker.last_size
            timestamp_ns = util.time_ns()  # pyright: ignore[reportAttributeAccessIssue]

            if last_trade and last_size:
                trade_data = {
                    "symbol": ticker.contract.symbol,
                    "last_trade": last_trade,
                    "last_size": last_size,
                    "timestamp_ns": timestamp_ns
                }

                # Track task for proper cleanup
                task = asyncio.create_task(self.kafka_producer.send_message("raw_trades", trade_data))  # pyright: ignore[reportAttributeAccessIssue]
                self._pending_tasks.append(task)
                
                # Clean completed tasks periodically
                self._pending_tasks = [t for t in self._pending_tasks if not t.done()]

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
            logger.error(f"Error in IBIngestor: {e}")
        finally:
            if self.ib.isConnected():
                self.ib.disconnect()
                logger.info("Disconnected from IB Gateway/TWS")
            
            # Cancel pending tasks on shutdown
            for task in self._pending_tasks:
                if not task.done():
                    task.cancel()
            if self._pending_tasks:
                await asyncio.gather(*self._pending_tasks, return_exceptions=True)
    
    async def close(self):
        """Async close with task cleanup"""
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IB Gateway/TWS")
        
        # Cancel and wait for pending tasks
        for task in self._pending_tasks:
            if not task.done():
                task.cancel()
        if self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
        self._pending_tasks = []
