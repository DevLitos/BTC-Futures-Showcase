import logging
import asyncio
import os
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from typing import Callable, Optional, Awaitable

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
            self._app = None
        else:
            try:
                self.bot = Bot(token=self.token)
                self._app = None  # Initialized later with start_command_listener
                self._force_exit_callback: Optional[Callable[[], Awaitable[None]]] = None
                self._shutdown_callback: Optional[Callable[[], Awaitable[None]]] = None
                self._command_listener_task = None
                logger.info("Bot de Telegram inicializado (token y chat_id cargados desde entorno).")
            except Exception as e:
                logger.error(f"No se pudo inicializar Telegram: {e}")
                self.bot = None
                self._app = None

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
        account_value = rec.get('account_value')
        score = rec.get('score')

        lines = [
            "📈 <b>Señal Detectada</b>",
            f"Símbolo: <b>{rec['symbol']}</b>",
            f"Dirección: <b>{rec['direction']}</b>",
            f"Estado: <b>{status}</b>",
            f"Precio: <b>{rec['entry_price']:.2f}</b>",
            f"Stop Loss: <b>{rec.get('stop_loss_price', 0):.2f}</b>",
        ]
        
        if score is not None:
            lines.append(f"📊 Score: <b>{score}/9</b>")
        
        if pnl is not None:
            lines.append(f"💰 PnL: <b>{pnl:.2f}</b>")
        
        if account_value is not None:
            lines.append(f"💼 Cuenta: <b>${account_value:.2f}</b>")
        
        lines.append(f"⏰ Timestamp: <b>{rec.get('signal_timestamp', 'N/A')}</b>")

        return "\n".join(lines)

    async def send_exit_notification(self, exit_data: dict):
        """Envia notificación de cierre de posición."""
        logger.info(f"Enviando notificación de salida para {exit_data['symbol']}...")
        if not self.bot:
            return

        message = self._format_exit_message(exit_data)

        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode=ParseMode.HTML
            )
            logger.info("Notificación de salida enviada correctamente.")
        except Exception as e:
            logger.error(f"Error enviando notificación de salida: {e}")

    def _format_exit_message(self, data: dict) -> str:
        """Formato enriquecido para cierres (incluye deltas y estimados)."""

        account_value = data.get('account_value')
        r_realized = data.get('r_realized')
        pnl = data.get('pnl')
        direction = data.get('direction', 'N/A')
        size = data.get('size')
        entry = data.get('entry_price')
        exit_price = data.get('exit_price')

        # Texto de dirección y tamaño
        dir_text = direction
        if size is not None:
            dir_text = f"{direction} {size:.6f}"

        lines = [
            "🚪 <b>Cierre de Posición</b>",
            f"Símbolo: <b>{data['symbol']}</b>",
            f"Dirección: <b>{dir_text}</b>",
            f"Razón: <b>{data.get('reason', 'N/A')}</b>",
        ]

        # Delta de precio
        if entry is not None and exit_price is not None and entry > 0:
            delta = exit_price - entry
            delta_pct = (delta / entry) * 100
            emoji = "📈" if delta > 0 else "📉" if delta < 0 else "➖"
            lines.append(
                f"{emoji} Entrada/Salida: <b>{entry:.2f} → {exit_price:.2f}</b> "
                f"({delta:+.2f}, {delta_pct:+.2f}%)"
            )
        else:
            lines.append("Entrada/Salida: <b>N/D</b>")

        # PnL real y estimado
        estimated_pnl = None
        if entry is not None and exit_price is not None and size is not None:
            side_mult = 1 if str(direction).upper() == 'LONG' else -1
            estimated_pnl = (exit_price - entry) * side_mult * size

        if pnl is not None:
            lines.append(f"💰 PnL: <b>{pnl:.2f}</b>")
            if estimated_pnl is not None and abs(pnl - estimated_pnl) > 0.01:
                lines.append(f"🧮 PnL estimado: <b>{estimated_pnl:.2f}</b>")
        elif estimated_pnl is not None:
            lines.append(f"🧮 PnL estimado: <b>{estimated_pnl:.2f}</b> (sin fill exacto)")
        else:
            lines.append("💰 PnL: <b>N/D</b>")

        # R realizado
        if r_realized is not None:
            emoji = "✅" if r_realized > 0 else "❌" if r_realized < 0 else "➖"
            lines.append(f"{emoji} R Realizado: <b>{r_realized:.2f}R</b>")
        else:
            lines.append("➖ R Realizado: <b>N/D</b>")

        # Precio de salida
        if exit_price is not None:
            lines.append(f"Precio Salida: <b>{exit_price:.2f}</b>")
        else:
            lines.append("Precio Salida: <b>N/D</b>")

        if account_value is not None:
            lines.append(f"💼 Cuenta Actual: <b>${account_value:.2f}</b>")

        lines.append(f"⏰ Timestamp: <b>{data.get('timestamp', 'N/A')}</b>")
        return "\n".join(lines)

    async def send_critical_alert(self, alert_data):
        """
        Send critical alert (errors, kill switch, etc.)
        Used for: 3 consecutive errors, daily loss limit, heartbeat death
        
        Args:
            alert_data: Either a string message or a dict with 'reason' key
        """
        # Handle both string and dict
        if isinstance(alert_data, str):
            reason = alert_data
            alert_data = {'reason': reason}
        else:
            reason = alert_data.get('reason', 'UNKNOWN')
        
        logger.critical(f"🚨 CRITICAL ALERT: {reason}")
        if not self.bot:
            logger.warning("Telegram disabled: cannot send critical alert")
            return

        message = self._format_critical_alert(alert_data)

        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode=ParseMode.HTML
            )
            logger.info("Critical alert sent to Telegram.")
        except Exception as e:
            logger.error(f"Error sending critical alert: {e}")

    def _format_critical_alert(self, data: dict) -> str:
        lines = [
            "🚨🚨🚨 <b>CRITICAL ALERT</b> 🚨🚨🚨",
            "",
            f"<b>Reason:</b> {data.get('reason', 'UNKNOWN')}",
            f"<b>Details:</b> {data.get('details', 'N/A')}",
            f"<b>Action:</b> {data.get('action', 'BOT STOPPED')}",
            "",
            f"⏰ <b>Time:</b> {data.get('timestamp', 'N/A')}",
            "",
            "⚠️ <i>Manual intervention may be required</i>"
        ]
        return "\n".join(lines)

    # ═══════════════════════════════════════════════════════════════════════════
    # TELEGRAM COMMAND LISTENER - /stop, /force_stop
    # ═══════════════════════════════════════════════════════════════════════════
    
    def set_force_exit_callback(self, callback: Callable[[], Awaitable[None]]):
        """
        Set the callback function to be called when /stop command is received.
        
        Args:
            callback: Async function that performs emergency close of all positions
        """
        self._force_exit_callback = callback
        logger.info("✅ Force exit callback registered for /stop command")
    
    def set_shutdown_callback(self, callback: Callable[[], Awaitable[None]]):
        """
        Set the callback function for /force_stop command (full shutdown).
        
        Args:
            callback: Async function that performs graceful shutdown of the app
        """
        self._shutdown_callback = callback
        logger.info("✅ Shutdown callback registered for /force_stop command")
    
    async def _handle_stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handle /stop command from Telegram - force exit all positions AND stop trading.
        
        This command:
        1. Closes all open positions via market order
        2. Activates the kill switch to prevent new trades
        3. Bot keeps running but won't open new positions
        """
        user_chat_id = str(update.effective_chat.id)
        
        # Security: Only allow commands from authorized chat_id
        if user_chat_id != str(self.chat_id):
            logger.warning(f"⚠️ Unauthorized /stop command from chat_id: {user_chat_id}")
            await update.message.reply_text("❌ Unauthorized. Access denied.")
            return
        
        logger.critical("🛑 /stop COMMAND RECEIVED FROM TELEGRAM - FORCE EXIT + KILL SWITCH")
        
        await update.message.reply_text(
            "🛑 <b>STOP COMMAND RECEIVED</b>\n\n"
            "⏳ Closing all positions and activating kill switch...",
            parse_mode=ParseMode.HTML
        )
        
        # Step 1: Close all positions
        if self._force_exit_callback:
            try:
                await self._force_exit_callback()
                logger.info("✅ Positions closed via Telegram /stop command")
            except Exception as e:
                error_msg = str(e)[:200]
                logger.error(f"❌ Force exit failed: {e}")
                await update.message.reply_text(
                    f"⚠️ <b>WARNING</b>: Position close had issues\n\n"
                    f"Error: {error_msg}\n\n"
                    "Activating kill switch anyway...",
                    parse_mode=ParseMode.HTML
                )
        
        # Step 2: Activate kill switch to prevent new trades
        try:
            from core.kill_switch import get_kill_switch
            kill_switch = get_kill_switch()
            kill_switch.activate(
                reason="Telegram /stop command",
                activated_by="telegram_user",
                allow_deactivation=True
            )
            logger.critical("🔴 KILL SWITCH ACTIVATED via Telegram /stop")
            
            await update.message.reply_text(
                "✅ <b>STOP COMPLETE</b>\n\n"
                "• All positions closed\n"
                "• Kill switch ACTIVATED\n"
                "• No new trades will be opened\n\n"
                "🔄 Bot is still running (managing existing SL/TP).\n"
                "Use /force_stop to completely shutdown the bot.",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Failed to activate kill switch: {e}")
            await update.message.reply_text(
                f"⚠️ Positions closed but kill switch activation failed: {str(e)[:100]}",
                parse_mode=ParseMode.HTML
            )
    
    async def _handle_force_stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handle /force_stop command from Telegram - complete shutdown.
        
        This command:
        1. Does NOT close positions (leaves them as-is with SL/TP on exchange)
        2. Shuts down the entire application
        """
        user_chat_id = str(update.effective_chat.id)
        
        # Security: Only allow commands from authorized chat_id
        if user_chat_id != str(self.chat_id):
            logger.warning(f"⚠️ Unauthorized /force_stop command from chat_id: {user_chat_id}")
            await update.message.reply_text("❌ Unauthorized. Access denied.")
            return
        
        logger.critical("🛑🛑🛑 /force_stop COMMAND RECEIVED - FULL SHUTDOWN")
        
        await update.message.reply_text(
            "🛑🛑🛑 <b>FORCE STOP COMMAND RECEIVED</b>\n\n"
            "⚠️ Shutting down application...\n"
            "⚠️ Positions will NOT be closed!\n"
            "⚠️ SL/TP orders remain on exchange.",
            parse_mode=ParseMode.HTML
        )
        
        if self._shutdown_callback:
            try:
                # Give time for message to be sent
                await asyncio.sleep(0.5)
                logger.critical("🔴 Initiating full shutdown via Telegram /force_stop")
                await self._shutdown_callback()
            except Exception as e:
                logger.error(f"❌ Shutdown failed: {e}")
                await update.message.reply_text(
                    f"❌ Shutdown failed: {str(e)[:200]}",
                    parse_mode=ParseMode.HTML
                )
        else:
            logger.error("❌ No shutdown callback registered!")
            await update.message.reply_text(
                "❌ <b>ERROR</b>: Shutdown callback not configured.",
                parse_mode=ParseMode.HTML
            )
    
    async def _handle_status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command from Telegram"""
        user_chat_id = str(update.effective_chat.id)
        
        if user_chat_id != str(self.chat_id):
            logger.warning(f"⚠️ Unauthorized /status command from chat_id: {user_chat_id}")
            return
        
        # Check kill switch status
        try:
            from core.kill_switch import get_kill_switch
            ks = get_kill_switch()
            ks_status = "🔴 ACTIVE" if ks.is_active else "🟢 Inactive"
            ks_reason = f"\nReason: {ks.reason}" if ks.is_active else ""
        except Exception:
            ks_status = "❓ Unknown"
            ks_reason = ""
        
        await update.message.reply_text(
            f"✅ <b>Bot is running</b>\n\n"
            f"Kill Switch: {ks_status}{ks_reason}\n\n"
            "Available commands:\n"
            "• /stop - Close positions + activate kill switch\n"
            "• /force_stop - Shutdown app (keeps positions)\n"
            "• /status - Show this message",
            parse_mode=ParseMode.HTML
        )
    
    async def start_command_listener(self):
        """
        Start the Telegram command listener for /stop, /force_stop and other commands.
        This runs as a background task alongside the main trading loop.
        """
        if not self.bot or not self.token:
            logger.warning("Cannot start command listener: Telegram not configured")
            return
        
        try:
            # Build the Application with the bot token
            self._app = Application.builder().token(self.token).build()
            
            # Register command handlers
            self._app.add_handler(CommandHandler("stop", self._handle_stop_command))
            self._app.add_handler(CommandHandler("force_stop", self._handle_force_stop_command))
            self._app.add_handler(CommandHandler("status", self._handle_status_command))
            
            logger.info("🎧 Starting Telegram command listener (polling mode)...")
            
            # Initialize and start polling
            await self._app.initialize()
            await self._app.start()
            await self._app.updater.start_polling(
                drop_pending_updates=True,  # Ignore old commands from before restart
                allowed_updates=["message"]
            )
            
            logger.info("✅ Telegram command listener active - /stop, /force_stop ready")
            
        except Exception as e:
            logger.error(f"Failed to start Telegram command listener: {e}")
    
    async def stop_command_listener(self):
        """Stop the Telegram command listener gracefully"""
        if self._app:
            try:
                logger.info("Stopping Telegram command listener...")
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
                logger.info("✅ Telegram command listener stopped")
            except Exception as e:
                logger.error(f"Error stopping command listener: {e}")
