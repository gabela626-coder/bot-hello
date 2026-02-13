"""Telegram bot — ShiftBot.

Reads BOT_TOKEN from environment, connects to Telegram API.
Accepts a Markdown schedule table, parses it, stores in memory,
and returns filtered views by /week and /month commands.
"""

import io
import logging
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

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
# PNG Renderer
# ---------------------------------------------------------------------------

# Color mapping for cell backgrounds
_COLOR_12 = "#a8e6a3"   # light green  — 12-hour shift
_COLOR_8 = "#fff3a3"    # light yellow — 8-hour shift
_COLOR_OFF = "#e0e0e0"  # grey         — day off / missing


def _cell_color(value: str) -> str:
    """Return background color for a cell value."""
    if value == "12":
        return _COLOR_12
    if value == "8":
        return _COLOR_8
    return _COLOR_OFF


def render_schedule_png(
    data: dict[str, dict[int, int]],
    day_start: int,
    day_end: int,
) -> io.BytesIO:
    """Render schedule as a colored PNG table and return as BytesIO stream.

    Args:
        data: schedule dict  {"Name": {day: hours, ...}, ...}
        day_start: first day number (inclusive)
        day_end: last day number (inclusive)

    Returns:
        BytesIO with the PNG image, seeked to 0.
    """
    days = list(range(day_start, day_end + 1))
    names = list(data.keys())
    n_rows = len(names)
    n_cols = len(days)

    # Build cell text and colors
    cell_text: list[list[str]] = []
    cell_colors: list[list[str]] = []
    for name in names:
        hours = data[name]
        row_text: list[str] = []
        row_colors: list[str] = []
        for d in days:
            val = hours.get(d)
            txt = str(val) if val is not None else "-"
            row_text.append(txt)
            row_colors.append(_cell_color(txt))
        cell_text.append(row_text)
        cell_colors.append(row_colors)

    # Dynamic figure size
    fig_width = max(n_cols * 1.2, 4)
    fig_height = max(n_rows * 0.6, 2) + 1.0  # extra space for legend

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ax.axis("off")
    ax.set_title(
        f"График {day_start}–{day_end}",
        fontsize=14,
        fontweight="bold",
        pad=12,
    )

    col_labels = [str(d) for d in days]
    row_labels = names

    table = ax.table(
        cellText=cell_text,
        cellColours=cell_colors,
        rowLabels=row_labels,
        colLabels=col_labels,
        loc="center",
        cellLoc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1.0, 1.6)

    # Style header row
    for j in range(n_cols):
        cell = table[0, j]
        cell.set_text_props(fontweight="bold")
        cell.set_facecolor("#d0d0d0")

    # Style row labels
    for i in range(n_rows):
        cell = table[i + 1, -1]
        cell.set_text_props(fontweight="bold")

    # Legend below the table
    legend_patches = [
        mpatches.Patch(facecolor=_COLOR_12, edgecolor="black", label="12 часов"),
        mpatches.Patch(facecolor=_COLOR_8, edgecolor="black", label="8 часов"),
        mpatches.Patch(facecolor=_COLOR_OFF, edgecolor="black", label="выходной"),
    ]
    fig.legend(
        handles=legend_patches,
        loc="lower center",
        ncol=3,
        fontsize=9,
        frameon=False,
    )

    fig.tight_layout(rect=[0, 0.06, 1, 1])

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)

    logger.info("PNG rendered: %d rows, %d days", n_rows, n_cols)
    return buf


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
    img = render_schedule_png(data, day_start, day_end)
    await update.message.reply_photo(photo=img)


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

    img = render_schedule_png(data, day_start, day_end)
    await update.message.reply_photo(photo=img)


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
