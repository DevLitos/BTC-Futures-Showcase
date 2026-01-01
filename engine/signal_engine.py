import asyncio
import numpy as np
from aiokafka import AIOKafkaConsumer
from prometheus_client import Counter
import logging

logger = logging.getLogger(__name__)

SIGNALS_GENERATED = Counter(
    'trading_signals_generated_total',
    'Número total de señales generadas',
    ['symbol', 'direction']
)

class SignalEngine:
    def __init__(self, kafka_topic: str, kafka_servers: str, risk_engine):
        self.kafka_topic = kafka_topic
        self.kafka_servers = kafka_servers
        self.consumer = None
        self.risk_engine = risk_engine

        # Estados internos
        self.symbol_states = {}
        self.signal_state = {}  # { symbol: {"last_signal": (direction, price, ts)} }

        # Parámetros del filtro anti-spam
        self.signal_cooldown_seconds = 1.0
        self.price_tolerance = 0.5

        self.minimum_bars = 300  # Warm-up adecuado para EMA200
        self.is_running = False

    async def start(self):
        logger.info("Iniciando Signal Engine...")

        self.consumer = AIOKafkaConsumer(
            self.kafka_topic,
            bootstrap_servers=self.kafka_servers,
            enable_auto_commit=True,
            auto_offset_reset="latest",
            value_deserializer=lambda x: x.decode('utf-8')
        )
        await self.consumer.start()

        self.is_running = True
        logger.info("Signal Engine iniciado y escuchando señales de mercado.")

        asyncio.create_task(self._consume_loop())

    async def stop(self):
        self.is_running = False
        if self.consumer:
            await self.consumer.stop()

    async def _consume_loop(self):
        while self.is_running:
            try:
                async for msg in self.consumer:
                    try:
                        await self._process_raw_message(msg.value)
                    except Exception as e:
                        logger.error(f"Error procesando mensaje en SignalEngine: {e}")

            except Exception as e:
                logger.error(f"Error en loop principal SignalEngine: {e}")
                await asyncio.sleep(1)

    async def _process_raw_message(self, raw_msg: str):
        """
        Espera recibir JSON como string con:
        {
            "symbol": "ETH/USDT",
            "price": 3134.86,
            "timestamp": 1733350802000000000
        }
        """
        try:
            import json
            data = json.loads(raw_msg)
        except Exception as e:
            logger.error(f"Mensaje inválido (no JSON): {e}")
            return

        symbol = data.get("symbol")
        price = float(data.get("price", 0))

        if not symbol or price <= 0:
            return

        state = self.symbol_states.setdefault(symbol, {
            "prices": [],
            "ema_200": None,
            "rsi": None
        })

        state["prices"].append(price)
        if len(state["prices"]) > 2000:
            state["prices"] = state["prices"][-2000:]

        if len(state["prices"]) < self.minimum_bars:
            return

        state["ema_200"] = self._calc_ema(state["prices"], 200)
        state["rsi"] = self._calc_rsi(state["prices"], 14)

        self._apply_strategy(symbol, price, state)

    def _apply_strategy(self, symbol: str, current_price: float, state: dict):
        ema_200 = state["ema_200"]
        rsi = state["rsi"]

        if ema_200 is None or np.isnan(ema_200):
            return
        if rsi is None or np.isnan(rsi):
            return

        # ----------- Señal de COMPRA -----------
        if current_price > ema_200 and rsi < 30:
            from datetime import datetime

            now_ts = asyncio.get_running_loop().time()
            signal_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            last = self.signal_state.get(symbol, {}).get("last_signal")
            should_emit = True

            if last:
                last_dir, last_price, last_ts = last
                price_diff = abs(current_price - last_price)
                time_diff = now_ts - last_ts

                if last_dir == "BUY":
                    if price_diff <= self.price_tolerance and time_diff < self.signal_cooldown_seconds:
                        should_emit = False
                        logger.debug(
                            f"Señal BUY suprimida para {symbol}: Δprecio={price_diff:.3f}, Δt={time_diff:.3f}s"
                        )

            if should_emit:
                logger.info(
                    f"SEÑAL DE COMPRA DETECTADA para {symbol} a ${current_price:.2f}"
                )

                preliminary_signal = {
                    "symbol": symbol,
                    "direction": "BUY",
                    "entry_price": current_price,
                    "signal_timestamp": signal_timestamp
                }

                self.signal_state.setdefault(symbol, {})["last_signal"] = (
                    "BUY", current_price, now_ts
                )

                asyncio.create_task(
                    self.risk_engine.process_signal(preliminary_signal)
                )

                SIGNALS_GENERATED.labels(
                    symbol=symbol,
                    direction="BUY"
                ).inc()

        # ----------- Señal de VENTA (cierre de largo) -----------
        if current_price < ema_200 and rsi > 70:
            from datetime import datetime

            now_ts = asyncio.get_running_loop().time()
            signal_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            last = self.signal_state.get(symbol, {}).get("last_signal")
            should_emit = True

            if last:
                last_dir, last_price, last_ts = last
                price_diff = abs(current_price - last_price)
                time_diff = now_ts - last_ts

                if last_dir == "SELL":
                    if price_diff <= self.price_tolerance and time_diff < self.signal_cooldown_seconds:
                        should_emit = False
                        logger.debug(
                            f"Señal SELL suprimida para {symbol}: Δprecio={price_diff:.3f}, Δt={time_diff:.3f}s"
                        )

            if should_emit:
                logger.info(
                    f"SEÑAL DE VENTA DETECTADA para {symbol} a ${current_price:.2f}"
                )

                preliminary_signal = {
                    "symbol": symbol,
                    "direction": "SELL",
                    "entry_price": current_price,
                    "signal_timestamp": signal_timestamp
                }

                self.signal_state.setdefault(symbol, {})["last_signal"] = (
                    "SELL", current_price, now_ts
                )

                asyncio.create_task(
                    self.risk_engine.process_signal(preliminary_signal)
                )

                SIGNALS_GENERATED.labels(
                    symbol=symbol,
                    direction="SELL"
                ).inc()


    # -------------------------
    # Utilidades de indicadores
    # -------------------------

    def _calc_ema(self, prices, period):
        prices_np = np.array(prices)
        alpha = 2 / (period + 1)
        ema = prices_np[0]

        for price in prices_np[1:]:
            ema = alpha * price + (1 - alpha) * ema

        return ema

    def _calc_rsi(self, prices, period=14):
        prices_np = np.array(prices)
        diffs = np.diff(prices_np)

        gains = np.where(diffs > 0, diffs, 0)
        losses = np.where(diffs < 0, -diffs, 0)

        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
