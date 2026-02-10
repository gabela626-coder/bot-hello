"""Telegram bot — bot-hello.

Reads BOT_TOKEN from environment, connects to Telegram API,
handles /start and echoes any other text message.
"""

import logging
import os
import sys

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("bot-hello")


async def start(update: Update, context) -> None:
    """Handle /start command."""
    user = update.effective_user
    logger.info("/start from %s (id=%s)", user.username, user.id)
    await update.message.reply_text(
        f"Привет, {user.first_name}! Я bot-hello 🤖"
    )


async def echo(update: Update, context) -> None:
    """Echo any text message back to the user."""
    text = update.message.text
    user = update.effective_user
    logger.info("Message from %s (id=%s): %s", user.username, user.id, text)
    await update.message.reply_text(text)


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN environment variable is not set!")
        sys.exit(1)

    logger.info("Starting bot-hello...")
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    logger.info("Bot is polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
