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
import matplotlib.patches as patches
from matplotlib.backends.backend_agg import FigureCanvasAgg

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
# PNG Renderer — custom SaaS-style using patches.Rectangle
# ---------------------------------------------------------------------------

COLORS = {
    "shift_24": "#b8d4f0",
    "day_12": "#a8e6a3",
    "night_12": "#a3c8ff",
    "shift_8": "#fff3a3",
    "off": "#f2f2f2",
    "header": "#e9ecef",
    "weekend": "#f7f7f7",
    "today": "#ffd6d6",
    "grid": "#cccccc",
    "text": "#222222",
}

# Layout constants (inches in data-coordinate space)
_CELL_W = 1.0
_CELL_H = 0.45
_LEFT_M = 2.2    # name column width
_RIGHT_M = 1.2   # total-hours column width
_TOP_M = 1.2     # space above header for title
_BOT_M = 1.0     # space below table for legend
_DPI = 200


def _shift_cell_color(entry: ShiftEntry | None) -> str:
    """Return background color for a shift entry."""
    if entry is None or entry["type"] == "off":
        return COLORS["off"]
    if entry["type"] == "night":
        return COLORS["night_12"]
    if entry["hours"] == 24:
        return COLORS["shift_24"]
    if entry["hours"] == 12:
        return COLORS["day_12"]
    if entry["hours"] == 8:
        return COLORS["shift_8"]
    return COLORS["off"]


def _shift_cell_text(entry: ShiftEntry | None) -> str:
    """Return display text for a shift entry."""
    if entry is None or entry["type"] == "off":
        return "-"
    if entry["type"] == "night":
        return f"{entry['hours']}н"
    return str(entry["hours"])


def _current_month_label() -> str:
    """Return current month and year as a Russian string."""
    now = datetime.now()
    return f"{_MONTH_NAMES_RU[now.month]} {now.year}"


def _draw_cell(
    ax: plt.Axes,
    x: float,
    y: float,
    w: float,
    h: float,
    bg: str,
    text: str,
    *,
    fontsize: int = 9,
    bold: bool = False,
    ha: str = "center",
    text_pad: float = 0.0,
) -> None:
    """Draw a single cell rectangle with centered text."""
    rect = patches.Rectangle(
        (x, y), w, h,
        facecolor=bg,
        edgecolor=COLORS["grid"],
        linewidth=0.5,
    )
    ax.add_patch(rect)
    tx = (x + text_pad + w / 2) if ha == "center" else (x + 0.15)
    ax.text(
        tx,
        y + h / 2,
        text,
        ha=ha,
        va="center",
        fontsize=fontsize,
        fontweight="bold" if bold else "normal",
        color=COLORS["text"],
        clip_on=True,
    )


def render_schedule_png(
    data: dict[str, dict[int, ShiftEntry]],
    day_start: int,
    day_end: int,
) -> io.BytesIO:
    """Render schedule as a SaaS-style colored PNG and return as BytesIO."""
    days = list(range(day_start, day_end + 1))
    names = list(data.keys())
    n_rows = len(names)
    n_cols = len(days)

    now = datetime.now()
    today = now.day
    year, month = now.year, now.month

    # --- Figure dimensions ------------------------------------------------
    fig_w = max(_LEFT_M + n_cols * _CELL_W + _RIGHT_M, 8.0)
    fig_h = _TOP_M + (n_rows + 1) * _CELL_H + _BOT_M

    fig = plt.figure(figsize=(fig_w, fig_h), dpi=_DPI)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    # --- Title ------------------------------------------------------------
    month_label = _current_month_label()
    ax.text(
        fig_w / 2,
        fig_h - 0.45,
        f"График {day_start}–{day_end} ({month_label})",
        ha="center",
        va="center",
        fontsize=16,
        fontweight="bold",
        color=COLORS["text"],
    )

    # y0 = top edge of the header row
    y0 = fig_h - _TOP_M

    # --- Header row: "Имя" column -----------------------------------------
    _draw_cell(
        ax, 0, y0 - _CELL_H, _LEFT_M, _CELL_H,
        COLORS["header"], "Имя",
        fontsize=10, bold=True, ha="left",
    )

    # --- Header row: day columns ------------------------------------------
    for j, d in enumerate(days):
        x = _LEFT_M + j * _CELL_W

        # Determine header background
        is_today = d == today
        try:
            wd = calendar.weekday(year, month, d)
            is_weekend = wd >= 5
        except ValueError:
            is_weekend = False

        if is_today:
            bg = COLORS["today"]
        elif is_weekend:
            bg = COLORS["weekend"]
        else:
            bg = COLORS["header"]

        _draw_cell(
            ax, x, y0 - _CELL_H, _CELL_W, _CELL_H,
            bg, str(d),
            fontsize=10, bold=True,
        )

    # --- Header row: "Итого" column ---------------------------------------
    total_x = _LEFT_M + n_cols * _CELL_W
    _draw_cell(
        ax, total_x, y0 - _CELL_H, _RIGHT_M, _CELL_H,
        COLORS["header"], "Итого",
        fontsize=10, bold=True,
    )

    # --- Data rows --------------------------------------------------------
    for i, name in enumerate(names):
        shifts = data[name]
        ry = y0 - (i + 2) * _CELL_H  # bottom of this row

        # Name cell
        _draw_cell(
            ax, 0, ry, _LEFT_M, _CELL_H,
            "white", name,
            fontsize=10, bold=True, ha="left",
        )

        # Shift cells
        row_total = 0
        for j, d in enumerate(days):
            x = _LEFT_M + j * _CELL_W
            entry = shifts.get(d)
            bg = _shift_cell_color(entry)
            txt = _shift_cell_text(entry)

            if entry and entry["type"] != "off":
                row_total += entry["hours"]

            _draw_cell(ax, x, ry, _CELL_W, _CELL_H, bg, txt, fontsize=9)

        # Total hours cell
        _draw_cell(
            ax, total_x, ry, _RIGHT_M, _CELL_H,
            "white", f"{row_total}ч",
            fontsize=10, bold=True,
        )

    # --- Legend ------------------------------------------------------------
    legend_items = [
        (COLORS["shift_24"], "24 часа"),
        (COLORS["day_12"], "12 дневная"),
        (COLORS["night_12"], "12 ночная"),
        (COLORS["shift_8"], "8 часов"),
        (COLORS["off"], "выходной"),
    ]
    n_leg = len(legend_items)
    leg_spacing = min(2.8, (fig_w - 0.6) / n_leg)
    total_leg_w = n_leg * leg_spacing
    leg_x0 = (fig_w - total_leg_w) / 2
    leg_y = 0.35

    for idx, (color, label) in enumerate(legend_items):
        lx = leg_x0 + idx * leg_spacing
        # Color swatch
        swatch = patches.Rectangle(
            (lx, leg_y), 0.35, 0.25,
            facecolor=color,
            edgecolor=COLORS["grid"],
            linewidth=0.5,
        )
        ax.add_patch(swatch)
        # Label text
        ax.text(
            lx + 0.5, leg_y + 0.125,
            label,
            ha="left",
            va="center",
            fontsize=9,
            color=COLORS["text"],
        )

    # --- Save to buffer ---------------------------------------------------
    buf = io.BytesIO()
    canvas = FigureCanvasAgg(fig)
    canvas.print_png(buf)
    buf.seek(0)
    plt.close(fig)

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
        "  24 — суточная 24ч\n"
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
