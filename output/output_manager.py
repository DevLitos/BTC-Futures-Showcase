import logging
import asyncio
import os
from telegram import Bot
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)


# Nota: leemos las variables de entorno en tiempo de construcción, no en import,
# para capturar valores actualizados si el módulo ya estaba cargado.

class OutputManager:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "TU_BOT_TOKEN_AQUI")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "TU_CHAT_ID_AQUI")

        placeholder_tokens = {"TU_BOT_TOKEN_AQUI", "", None}
        placeholder_chats = {"TU_CHAT_ID_AQUI", "", None}

        if self.token in placeholder_tokens or self.chat_id in placeholder_chats:
            logger.warning("Token o Chat ID de Telegram parecen placeholders. Telegram deshabilitado.")
            self.bot = None
        else:
            try:
                self.bot = Bot(token=self.token)
                logger.info("Bot de Telegram inicializado (token y chat_id cargados desde entorno).")
            except Exception as e:
                logger.error(f"No se pudo inicializar Telegram: {e}")
                self.bot = None

    async def send_recommendation(self, recommendation: dict):
        logger.info(f"Enviando recomendación para {recommendation['symbol']}...")
        if not self.bot:
            logger.warning("Telegram deshabilitado: bot no inicializado (revisa tokens/chat_id).")
            return

        message = self._format_telegram_message(recommendation)

        max_retries = 3
        base_backoff = 0.5

        for attempt in range(1, max_retries + 1):
            try:
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=message,
                    parse_mode=ParseMode.HTML
                )
                logger.info("Mensaje de Telegram enviado correctamente.")
                break

            except Exception as e:
                logger.error(f"Error enviando mensaje (intento {attempt}): {e}")

                if attempt == max_retries:
                    logger.error("Se alcanzó el máximo de reintentos. Continuando ejecución.")
                    break

                await asyncio.sleep(base_backoff * (2 ** (attempt - 1)))

    async def send_test_message(self, text: str = "Test de conexión Telegram"):
        """Envia un mensaje de prueba manual para verificar credenciales."""
        if not self.bot:
            logger.warning("Telegram deshabilitado: bot no inicializado (revisa tokens/chat_id).")
            return

        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.HTML
            )
            logger.info("Mensaje de prueba enviado a Telegram correctamente.")
        except Exception as e:
            logger.error(f"Error enviando mensaje de prueba a Telegram: {e}")

    def _format_telegram_message(self, rec: dict) -> str:
        status = rec.get('status', 'N/A')
        pnl = rec.get('pnl')

        lines = [
            "📈 <b>Señal Detectada</b>",
            f"Símbolo: <b>{rec['symbol']}</b>",
            f"Dirección: <b>{rec['direction']}</b>",
            f"Estado: <b>{status}</b>",
            f"Precio: <b>{rec['entry_price']:.2f}</b>",
            f"Stop Loss: <b>{rec.get('stop_loss_price', 0):.2f}</b>",
            f"⏰ Timestamp: <b>{rec.get('signal_timestamp', 'N/A')}</b>"
        ]

        if pnl is not None:
            lines.append(f"💰 PnL: <b>{pnl:.2f}</b>")

        return "\n".join(lines)
