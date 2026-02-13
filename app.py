"""Telegram bot — ShiftBot (Production-Level).

Reads BOT_TOKEN from environment, connects to Telegram API.
Accepts a Markdown schedule table, parses it (including night shifts),
stores in memory, and returns colored PNG views via inline buttons.
"""

import calendar
import io
import logging
import os
import sys
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
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

# Russian month names (nominative case for titles)
_MONTH_NAMES_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}

# Type alias for a single shift entry
ShiftEntry = dict  # {"type": "day"|"night"|"off", "hours": int}

# In-memory storage: {chat_id: {"Name": {day_int: ShiftEntry}}}
schedule_data: dict[int, dict[str, dict[int, ShiftEntry]]] = {}

# Set of chat_ids currently awaiting a Markdown table upload
awaiting_upload: set[int] = set()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _parse_cell(raw: str) -> ShiftEntry:
    """Parse a single cell value into a ShiftEntry.

    Rules:
        "12н" / "12Н"       -> {"type": "night", "hours": 12}
        "8", "12", etc.     -> {"type": "day",   "hours": N}
        "-", "", whitespace -> {"type": "off",   "hours": 0}
    """
    val = raw.strip()
    if not val or val == "-":
        return {"type": "off", "hours": 0}
    if "н" in val.lower():
        digits = "".join(c for c in val if c.isdigit())
        return {"type": "night", "hours": int(digits) if digits else 12}
    try:
        return {"type": "day", "hours": int(val)}
    except ValueError:
        return {"type": "off", "hours": 0}


def parse_schedule(text: str) -> dict[str, dict[int, ShiftEntry]]:
    """Parse a Markdown table into a schedule dict.

    Expected format:
        | Name | 1  | 2  | 3   |
        |------|----|----|-----|
        | Иван | 8  | 12н| 12  |

    Returns:
        {"Иван": {1: {"type":"day","hours":8}, 2: {"type":"night","hours":12}, ...}}

    Raises:
        ValueError: if no valid data rows are found.
    """
    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    result: dict[str, dict[int, ShiftEntry]] = {}
    header: list[str] | None = None

    for line in lines:
        if not line.startswith("|"):
            continue

        cells = [c.strip() for c in line.split("|")[1:-1]]

        if header is None:
            header = cells
            continue

        # Skip separator row like |---|---|---|
        if all(set(c) <= set("-: ") for c in cells):
            continue

        if not cells:
            continue

        name = cells[0]
        days: dict[int, ShiftEntry] = {}
        for i, val in enumerate(cells[1:], start=1):
            try:
                day_num = int(header[i])
            except (ValueError, IndexError):
                continue
            days[day_num] = _parse_cell(val)
        result[name] = days

    if not result:
        raise ValueError("Не удалось найти данные в таблице.")
    return result


# ---------------------------------------------------------------------------
# Formatter (text fallback, kept for compatibility)
# ---------------------------------------------------------------------------

def format_schedule(
    data: dict[str, dict[int, ShiftEntry]],
    day_start: int,
    day_end: int,
) -> str:
    """Format schedule data as a Markdown table for the given day range."""
    days = list(range(day_start, day_end + 1))

    header = "| Имя | " + " | ".join(str(d) for d in days) + " |"
    separator = "|-----|" + "|".join("---" for _ in days) + "|"

    rows: list[str] = []
    for name, shifts in data.items():
        vals: list[str] = []
        for d in days:
            entry = shifts.get(d)
            if entry is None or entry["type"] == "off":
                vals.append("-")
            elif entry["type"] == "night":
                vals.append(f"{entry['hours']}н")
            else:
                vals.append(str(entry["hours"]))
        rows.append(f"| {name} | {' | '.join(vals)} |")

    return "\n".join([header, separator] + rows)


# ---------------------------------------------------------------------------
# PNG Renderer
# ---------------------------------------------------------------------------

# Color mapping for cell backgrounds
_COLOR_DAY12 = "#a8e6a3"   # light green  — 12-hour day shift
_COLOR_NIGHT = "#a3c8ff"   # light blue   — 12-hour night shift
_COLOR_8 = "#fff3a3"       # light yellow — 8-hour shift
_COLOR_OFF = "#e0e0e0"     # grey         — day off / missing
_COLOR_TODAY_HDR = "#ffcccc"  # light red — today column header highlight


def _cell_color(entry: ShiftEntry | None) -> str:
    """Return background color for a shift entry."""
    if entry is None or entry["type"] == "off":
        return _COLOR_OFF
    if entry["type"] == "night":
        return _COLOR_NIGHT
    # day shift
    if entry["hours"] == 12:
        return _COLOR_DAY12
    if entry["hours"] == 8:
        return _COLOR_8
    return _COLOR_OFF


def _cell_text(entry: ShiftEntry | None) -> str:
    """Return display text for a shift entry."""
    if entry is None or entry["type"] == "off":
        return "-"
    if entry["type"] == "night":
        return f"{entry['hours']}н"
    return str(entry["hours"])


def _current_month_label() -> str:
    """Return current month and year as a Russian string, e.g. 'Февраль 2026'."""
    now = datetime.now()
    return f"{_MONTH_NAMES_RU[now.month]} {now.year}"


def render_schedule_png(
    data: dict[str, dict[int, ShiftEntry]],
    day_start: int,
    day_end: int,
) -> io.BytesIO:
    """Render schedule as a colored PNG table and return as BytesIO stream."""
    days = list(range(day_start, day_end + 1))
    names = list(data.keys())
    n_rows = len(names)
    n_cols = len(days)
    today = datetime.now().day

    # Build cell text and colors
    cell_text: list[list[str]] = []
    cell_colors: list[list[str]] = []
    for name in names:
        shifts = data[name]
        row_text: list[str] = []
        row_colors: list[str] = []
        for d in days:
            entry = shifts.get(d)
            row_text.append(_cell_text(entry))
            row_colors.append(_cell_color(entry))
        cell_text.append(row_text)
        cell_colors.append(row_colors)

    # Dynamic figure size
    fig_width = max(n_cols * 1.2, 4)
    fig_height = max(n_rows * 0.6, 2) + 1.0

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ax.axis("off")

    month_label = _current_month_label()
    ax.set_title(
        f"График {day_start}–{day_end} ({month_label})",
        fontsize=14,
        fontweight="bold",
        pad=12,
    )

    col_labels = [str(d) for d in days]
    row_labels = names

    tbl = ax.table(
        cellText=cell_text,
        cellColours=cell_colors,
        rowLabels=row_labels,
        colLabels=col_labels,
        loc="center",
        cellLoc="center",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(10)
    tbl.scale(1.0, 1.6)

    # Style header row + highlight current day
    for j in range(n_cols):
        cell = tbl[0, j]
        cell.set_text_props(fontweight="bold")
        if days[j] == today:
            cell.set_facecolor(_COLOR_TODAY_HDR)
            cell.set_edgecolor("#cc0000")
            cell.set_linewidth(2)
        else:
            cell.set_facecolor("#d0d0d0")

    # Style row labels
    for i in range(n_rows):
        cell = tbl[i + 1, -1]
        cell.set_text_props(fontweight="bold")

    # Legend below the table
    legend_patches = [
        mpatches.Patch(facecolor=_COLOR_DAY12, edgecolor="black", label="12 дневная"),
        mpatches.Patch(facecolor=_COLOR_NIGHT, edgecolor="black", label="12 ночная"),
        mpatches.Patch(facecolor=_COLOR_8, edgecolor="black", label="8 часов"),
        mpatches.Patch(facecolor=_COLOR_OFF, edgecolor="black", label="выходной"),
    ]
    fig.legend(
        handles=legend_patches,
        loc="lower center",
        ncol=4,
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
# Inline keyboard helpers
# ---------------------------------------------------------------------------

def _week_ranges(max_day: int) -> list[tuple[int, int]]:
    """Return list of (start, end) tuples for week ranges up to max_day."""
    ranges: list[tuple[int, int]] = []
    start = 1
    while start <= max_day:
        end = min(start + 6, max_day)
        ranges.append((start, end))
        start = end + 1
    return ranges


def _build_schedule_keyboard(max_day: int) -> InlineKeyboardMarkup:
    """Build an InlineKeyboardMarkup with week buttons + full month button."""
    weeks = _week_ranges(max_day)
    buttons: list[list[InlineKeyboardButton]] = []
    for i, (s, e) in enumerate(weeks, start=1):
        buttons.append([
            InlineKeyboardButton(
                text=f"Неделя {i} ({s}–{e})",
                callback_data=f"week_{s}_{e}",
            )
        ])
    buttons.append([
        InlineKeyboardButton(text="Весь месяц", callback_data="month")
    ])
    return InlineKeyboardMarkup(buttons)


def _max_day_in_data(data: dict[str, dict[int, ShiftEntry]]) -> int:
    """Return the highest day number present in schedule data."""
    all_days: set[int] = set()
    for shifts in data.values():
        all_days.update(shifts.keys())
    return max(all_days) if all_days else 0


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
        "/week — выбрать неделю (inline-кнопки)\n"
        "/month — показать график на весь месяц\n\n"
        "Поддерживаемые значения в таблице:\n"
        "  8 — дневная 8ч\n"
        "  12 — дневная 12ч\n"
        "  12н — ночная 12ч\n"
        "  - — выходной"
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
        "| Иван | 8 | 12н | 12 |"
    )


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /week — show inline keyboard to pick a week range."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    logger.info("/week from %s (id=%s, chat=%s)", user.username, user.id, chat_id)

    if chat_id not in schedule_data:
        await update.message.reply_text(
            "График ещё не загружен. Используйте /upload."
        )
        return

    data = schedule_data[chat_id]
    max_day = _max_day_in_data(data)
    if max_day == 0:
        await update.message.reply_text("В графике нет данных.")
        return

    keyboard = _build_schedule_keyboard(max_day)
    await update.message.reply_text("Выберите период:", reply_markup=keyboard)


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /month — return full month schedule as PNG."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    logger.info("/month from %s (id=%s, chat=%s)", user.username, user.id, chat_id)

    if chat_id not in schedule_data:
        await update.message.reply_text(
            "График ещё не загружен. Используйте /upload."
        )
        return

    data = schedule_data[chat_id]
    max_day = _max_day_in_data(data)
    if max_day == 0:
        await update.message.reply_text("В графике нет данных.")
        return

    img = render_schedule_png(data, 1, max_day)
    await update.message.reply_photo(photo=img)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button callbacks for week/month selection."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    user = update.effective_user
    cb_data = query.data
    logger.info(
        "Callback %r from %s (id=%s, chat=%s)",
        cb_data, user.username, user.id, chat_id,
    )

    if chat_id not in schedule_data:
        await query.edit_message_text("График ещё не загружен. Используйте /upload.")
        return

    data = schedule_data[chat_id]
    max_day = _max_day_in_data(data)
    if max_day == 0:
        await query.edit_message_text("В графике нет данных.")
        return

    if cb_data == "month":
        day_start, day_end = 1, max_day
    elif cb_data.startswith("week_"):
        parts = cb_data.split("_")
        try:
            day_start, day_end = int(parts[1]), int(parts[2])
        except (ValueError, IndexError):
            await query.edit_message_text("Ошибка: неверные данные кнопки.")
            return
    else:
        await query.edit_message_text("Неизвестная команда.")
        return

    try:
        img = render_schedule_png(data, day_start, day_end)
    except Exception as exc:
        logger.error("Failed to render PNG: %s", exc)
        await query.edit_message_text(f"Ошибка рендеринга: {exc}")
        return

    await query.message.reply_photo(photo=img)


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
            (len(shifts) for shifts in data.values()),
            default=0,
        )
        logger.info(
            "Schedule loaded for chat %s: %d people, %d days",
            chat_id, people, days_count,
        )

        # Determine max day for keyboard
        max_day = _max_day_in_data(data)
        keyboard = _build_schedule_keyboard(max_day)
        await update.message.reply_text(
            f"График загружен: {people} чел., {days_count} дн.\n"
            "Выберите период для просмотра:",
            reply_markup=keyboard,
        )
    else:
        logger.info(
            "Unrecognized text from %s (id=%s): %s",
            user.username, user.id, text[:80],
        )
        await update.message.reply_text(
            "Используйте /upload чтобы загрузить график,\n"
            "/week для выбора недели или /month для полного месяца."
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
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("ShiftBot is polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
