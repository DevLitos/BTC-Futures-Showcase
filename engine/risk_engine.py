# engine/risk_engine.py

import logging
import asyncio
from monitoring.metrics import ORDERS_RECOMMENDED, ORDERS_REJECTED

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class RiskEngine:
    """
    Valida señales, calcula el tamaño de la posición y gestiona el riesgo global.
    """
    def __init__(self, capital: float, risk_per_trade_pct: float, output_manager):
        self.capital = capital
        self.risk_per_trade_pct = risk_per_trade_pct
        self.output_manager = output_manager
        
        # Estado
        self.daily_pnl = 0.0
        self.open_positions = {}
        self.daily_loss_limit = - (capital * 0.05)

        # Para evitar race conditions
        self._lock = asyncio.Lock()

    async def process_signal(self, signal: dict):
        """
        Procesa una señal preliminar del SignalEngine.
        Protegido con lock porque puede ser llamado concurrentemente.
        """
        async with self._lock:

            symbol = signal.get("symbol")
            entry_price = signal.get("entry_price")
            direction = signal.get("direction")

            # Validación básica
            if not symbol or entry_price is None or direction not in ("BUY", "SELL"):
                ORDERS_REJECTED.labels(symbol=symbol or "UNKNOWN", reason="invalid_signal").inc()
                logger.warning(f"Señal inválida descartada: {signal}")
                return

            # 1. Reglas globales de riesgo
            if self.daily_pnl <= self.daily_loss_limit:
                ORDERS_REJECTED.labels(symbol=symbol, reason='daily_loss_limit').inc()
                logger.warning(f"SEÑAL DESCARTADA para {symbol}: Límite de pérdida diaria alcanzado.")
                return

            # 2. Si es SELL, intentar cerrar posición abierta
            if direction == "SELL":
                if symbol not in self.open_positions:
                    ORDERS_REJECTED.labels(symbol=symbol, reason='no_open_position').inc()
                    logger.warning(f"SEÑAL SELL DESCARTADA para {symbol}: No hay posición abierta para cerrar.")
                    return

                open_pos = self.open_positions[symbol]
                size = open_pos["order"]["size"]
                entry_open_price = open_pos["order"]["entry_price"]

                pnl = (entry_price - entry_open_price) * size  # cierre de largo
                self.daily_pnl += pnl

                close_order = {
                    "symbol": symbol,
                    "direction": "SELL",
                    "size": size,
                    "entry_price": entry_price,
                    "stop_loss_price": open_pos["order"].get("stop_loss_price"),
                    "status": "CLOSE_POSITION",
                    "signal_timestamp": signal.get("signal_timestamp", "N/A"),
                    "pnl": round(pnl, 2)
                }

                ORDERS_RECOMMENDED.labels(symbol=symbol, direction=direction).inc()
                logger.info(f"CIERRE DE POSICIÓN: {close_order}")

                del self.open_positions[symbol]

                if self.output_manager:
                    try:
                        await self.output_manager.send_recommendation(close_order)
                    except Exception as e:
                        logger.error(f"Error enviando cierre al OutputManager: {e}")
                else:
                    logger.warning("Output Manager no configurado. Solo se registró internamente.")

                return

            # 3. Ya existe posición (solo para BUY)
            if symbol in self.open_positions:
                ORDERS_REJECTED.labels(symbol=symbol, reason='already_open').inc()
                logger.warning(f"SEÑAL DESCARTADA para {symbol}: Ya existe una posición abierta.")
                return

            # 4. Calcular Stop-Loss para apertura BUY
            stop_loss_price = entry_price * 0.98

            risk_per_share = abs(entry_price - stop_loss_price)

            if risk_per_share == 0:
                ORDERS_REJECTED.labels(symbol=symbol, reason="zero_risk").inc()
                logger.warning(f"SEÑAL DESCARTADA para {symbol}: Riesgo por acción es cero.")
                return

            # 4. Calcular tamaño
            risk_amount = self.capital * self.risk_per_trade_pct
            position_size = risk_amount / risk_per_share

            logger.info(
                f"Cálculo de riesgo para {symbol}: Capital=${self.capital}, "
                f"Riesgo=${risk_amount:.2f}, SL=${stop_loss_price:.2f}, "
                f"Tamaño={position_size:.4f} unidades."
            )

            # 5. Recomendación final
            order = {
                "symbol": symbol,
                "direction": direction,
                "size": round(position_size, 4),
                "entry_price": entry_price,
                "stop_loss_price": stop_loss_price,
                 "status": "RECOMMENDED",
                 "signal_timestamp": signal.get("signal_timestamp", "N/A")
            }

            # 6. Guardar posición interna
            self.open_positions[symbol] = {
                "order": order,
                "risk_per_share": risk_per_share,
                "risk_usd": risk_amount,
                "timestamp": signal.get("signal_timestamp")
            }

            ORDERS_RECOMMENDED.labels(symbol=symbol, direction=direction).inc()
            logger.info(f"RECOMENDACIÓN DE ORDEN GENERADA: {order}")

            # 7. Enviar recomendación al output manager
            if self.output_manager:
                try:
                    await self.output_manager.send_recommendation(order)
                except Exception as e:
                    logger.error(f"Error enviando recomendación al OutputManager: {e}")
            else:
                logger.warning("Output Manager no configurado. Solo se registró internamente.")
