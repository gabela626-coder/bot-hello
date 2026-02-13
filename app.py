"""Telegram bot — ShiftBot.

Reads BOT_TOKEN from environment, connects to Telegram API.
Accepts a Markdown schedule table, parses it, stores in memory,
and returns filtered views by /week and /month commands.
"""

import logging
import os
import sys

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("shift-bot")

# In-memory storage: {chat_id: {"Name": {day_int: hours_int}}}
schedule_data: dict[int, dict[str, dict[int, int]]] = {}

# Set of chat_ids currently awaiting a Markdown table upload
awaiting_upload: set[int] = set()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_schedule(text: str) -> dict[str, dict[int, int]]:
    """Parse a Markdown table into a schedule dict.

    Expected format:
        | Name | 1 | 2 | 3 |
        |------|---|---|---|
        | Иван | 8 | 8 | 12 |

    Returns:
        {"Иван": {1: 8, 2: 8, 3: 12}}

    Raises:
        ValueError: if no valid data rows are found.
    """
    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    result: dict[str, dict[int, int]] = {}
    header: list[str] | None = None

    for line in lines:
        if not line.startswith("|"):
            continue

        cells = [c.strip() for c in line.split("|")[1:-1]]

        if header is None:
            header = cells  # e.g. ["Name", "1", "2", "3"]
            continue

        # Skip separator row like |---|---|---|
        if all(set(c) <= set("-: ") for c in cells):
            continue

        if not cells:
            continue

        name = cells[0]
        days: dict[int, int] = {}
        for i, val in enumerate(cells[1:], start=1):
            try:
                day_num = int(header[i])
                days[day_num] = int(val) if val else 0
            except (ValueError, IndexError):
                continue
        result[name] = days

    if not result:
        raise ValueError("Не удалось найти данные в таблице.")
    return result


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------

def format_schedule(
    data: dict[str, dict[int, int]],
    day_start: int,
    day_end: int,
) -> str:
    """Format schedule data as a Markdown table for the given day range."""
    days = list(range(day_start, day_end + 1))

    header = "| Имя | " + " | ".join(str(d) for d in days) + " |"
    separator = "|-----|" + "|".join("---" for _ in days) + "|"

    rows: list[str] = []
    for name, hours in data.items():
        vals = " | ".join(str(hours.get(d, "-")) for d in days)
        rows.append(f"| {name} | {vals} |")

    return "\n".join([header, separator] + rows)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — describe bot capabilities."""
    user = update.effective_user
    logger.info("/start from %s (id=%s)", user.username, user.id)
    await update.message.reply_text(
        "Привет! Я ShiftBot — бот для работы с графиком смен.\n\n"
        "Команды:\n"
        "/upload — загрузить график (Markdown-таблица)\n"
        "/week <start>-<end> — показать график на выбранные дни\n"
        "/month — показать график на весь месяц\n"
    )


async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /upload — put chat into 'awaiting table' mode."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    logger.info("/upload from %s (id=%s, chat=%s)", user.username, user.id, chat_id)
    awaiting_upload.add(chat_id)
    await update.message.reply_text(
        "Отправьте Markdown-таблицу с графиком в следующем сообщении.\n\n"
        "Пример:\n"
        "| Имя | 1 | 2 | 3 |\n"
        "|-----|---|---|---|\n"
        "| Иван | 8 | 8 | 12 |"
    )


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /week <start>-<end> — return schedule for a day range."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    logger.info("/week from %s (id=%s, chat=%s)", user.username, user.id, chat_id)

    if chat_id not in schedule_data:
        await update.message.reply_text(
            "График ещё не загружен. Используйте /upload."
        )
        return

    # Parse argument
    if not context.args:
        await update.message.reply_text(
            "Укажите диапазон дней. Пример: /week 1-7"
        )
        return

    arg = context.args[0]
    try:
        parts = arg.split("-")
        if len(parts) != 2:
            raise ValueError
        day_start, day_end = int(parts[0]), int(parts[1])
        if day_start > day_end or day_start < 1 or day_end > 31:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Неверный формат. Пример: /week 1-7"
        )
        return

    data = schedule_data[chat_id]
    table = format_schedule(data, day_start, day_end)
    await update.message.reply_text(
        f"График {day_start}-{day_end}:\n\n{table}",
        parse_mode=None,
    )


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /month — return full month schedule."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    logger.info("/month from %s (id=%s, chat=%s)", user.username, user.id, chat_id)

    if chat_id not in schedule_data:
        await update.message.reply_text(
            "График ещё не загружен. Используйте /upload."
        )
        return

    data = schedule_data[chat_id]

    # Determine full day range from the loaded data
    all_days: set[int] = set()
    for hours in data.values():
        all_days.update(hours.keys())

    if not all_days:
        await update.message.reply_text("В графике нет данных.")
        return

    day_start = min(all_days)
    day_end = max(all_days)

    table = format_schedule(data, day_start, day_end)
    await update.message.reply_text(
        f"График за месяц ({day_start}-{day_end}):\n\n{table}",
        parse_mode=None,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages — process table upload or show hint."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    text = update.message.text

    if chat_id in awaiting_upload:
        logger.info(
            "Receiving schedule table from %s (id=%s, chat=%s)",
            user.username, user.id, chat_id,
        )
        awaiting_upload.discard(chat_id)
        try:
            data = parse_schedule(text)
        except ValueError as exc:
            logger.error(
                "Failed to parse schedule from %s (chat=%s): %s",
                user.username, chat_id, exc,
            )
            await update.message.reply_text(
                f"Ошибка парсинга: {exc}\nПопробуйте /upload ещё раз."
            )
            return

        schedule_data[chat_id] = data
        people = len(data)
        days_count = max(
            (len(hours) for hours in data.values()),
            default=0,
        )
        logger.info(
            "Schedule loaded for chat %s: %d people, %d days",
            chat_id, people, days_count,
        )
        await update.message.reply_text(
            f"График загружен: {people} чел., {days_count} дн.\n"
            "Используйте /week или /month для просмотра."
        )
    else:
        logger.info(
            "Unrecognized text from %s (id=%s): %s",
            user.username, user.id, text[:80],
        )
        await update.message.reply_text(
            "Используйте /upload чтобы загрузить график,\n"
            "/week <start>-<end> или /month для просмотра."
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN environment variable is not set!")
        sys.exit(1)

    logger.info("Starting ShiftBot...")
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("upload", cmd_upload))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("ShiftBot is polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
