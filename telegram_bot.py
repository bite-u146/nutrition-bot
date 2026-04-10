import os
import logging
import re
import anthropic
import psycopg2
import psycopg2.pool
from contextlib import contextmanager
from datetime import datetime, date, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── PostgreSQL ───────────────────────────────────────────────────────────────

db_pool: psycopg2.pool.SimpleConnectionPool | None = None

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id          BIGINT PRIMARY KEY,
    timezone_offset  INTEGER NOT NULL DEFAULT 0,
    last_summary_sent DATE
);

CREATE TABLE IF NOT EXISTS diary_entries (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT  NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    entry_date  DATE    NOT NULL,
    entry_time  TIME    NOT NULL,
    dish        TEXT    NOT NULL,
    calories    REAL    NOT NULL DEFAULT 0,
    proteins    REAL    NOT NULL DEFAULT 0,
    fats        REAL    NOT NULL DEFAULT 0,
    carbs       REAL    NOT NULL DEFAULT 0,
    fiber       REAL    NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_diary_user_date ON diary_entries(user_id, entry_date);
"""


def init_db() -> None:
    global db_pool
    db_url = os.environ["DATABASE_URL"]
    # Railway иногда отдаёт postgres://, psycopg2 требует postgresql://
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, db_url)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLES_SQL)
        conn.commit()
    logger.info("База данных инициализирована.")


@contextmanager
def get_conn():
    conn = db_pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)


# ─── DB-операции ─────────────────────────────────────────────────────────────

def db_ensure_user(user_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (user_id,),
            )
        conn.commit()


def db_get_user(user_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT timezone_offset, last_summary_sent FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {"timezone_offset": row[0], "last_summary_sent": row[1]}


def db_set_timezone(user_id: int, offset: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET timezone_offset = %s WHERE user_id = %s",
                (offset, user_id),
            )
        conn.commit()


def db_delete_today_entries(user_id: int, entry_date: date) -> int:
    """Удаляет все записи пользователя за указанную дату. Возвращает кол-во удалённых строк."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM diary_entries WHERE user_id = %s AND entry_date = %s",
                (user_id, entry_date),
            )
            deleted = cur.rowcount
        conn.commit()
    return deleted


def db_add_entry(
    user_id: int,
    entry_date: date,
    entry_time: str,
    dish: str,
    nutrition: dict,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO diary_entries
                    (user_id, entry_date, entry_time, dish,
                     calories, proteins, fats, carbs, fiber)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id, entry_date, entry_time, dish,
                    nutrition["calories"], nutrition["proteins"],
                    nutrition["fats"],    nutrition["carbs"],
                    nutrition["fiber"],
                ),
            )
        conn.commit()


def db_get_day_entries(user_id: int, entry_date: date) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entry_time, dish, calories, proteins, fats, carbs, fiber
                FROM diary_entries
                WHERE user_id = %s AND entry_date = %s
                ORDER BY entry_time
                """,
                (user_id, entry_date),
            )
            rows = cur.fetchall()
    return [
        {
            "time": str(r[0])[:5],
            "dish": r[1],
            "calories": r[2], "proteins": r[3],
            "fats": r[4], "carbs": r[5], "fiber": r[6],
        }
        for r in rows
    ]


def db_get_day_total(user_id: int, entry_date: date) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*),
                    COALESCE(SUM(calories),0), COALESCE(SUM(proteins),0),
                    COALESCE(SUM(fats),0),     COALESCE(SUM(carbs),0),
                    COALESCE(SUM(fiber),0)
                FROM diary_entries
                WHERE user_id = %s AND entry_date = %s
                """,
                (user_id, entry_date),
            )
            row = cur.fetchone()
    if not row or row[0] == 0:
        return None
    return {
        "count":    row[0],
        "calories": round(row[1], 1),
        "proteins": round(row[2], 1),
        "fats":     round(row[3], 1),
        "carbs":    round(row[4], 1),
        "fiber":    round(row[5], 1),
    }


def db_get_history(user_id: int, start_date: date, end_date: date) -> list[dict]:
    """Суммы по каждому дню в диапазоне [start_date, end_date]."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    entry_date,
                    COUNT(*),
                    COALESCE(SUM(calories),0), COALESCE(SUM(proteins),0),
                    COALESCE(SUM(fats),0),     COALESCE(SUM(carbs),0),
                    COALESCE(SUM(fiber),0)
                FROM diary_entries
                WHERE user_id = %s AND entry_date BETWEEN %s AND %s
                GROUP BY entry_date
                ORDER BY entry_date DESC
                """,
                (user_id, start_date, end_date),
            )
            rows = cur.fetchall()
    return [
        {
            "date":     r[0],
            "count":    r[1],
            "calories": round(r[2], 1),
            "proteins": round(r[3], 1),
            "fats":     round(r[4], 1),
            "carbs":    round(r[5], 1),
            "fiber":    round(r[6], 1),
        }
        for r in rows
    ]


def db_get_all_users() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, timezone_offset, last_summary_sent FROM users")
            rows = cur.fetchall()
    return [
        {"user_id": r[0], "timezone_offset": r[1], "last_summary_sent": r[2]}
        for r in rows
    ]


def db_mark_summary_sent(user_id: int, sent_date: date) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET last_summary_sent = %s WHERE user_id = %s",
                (sent_date, user_id),
            )
        conn.commit()


# ─── Bot logic ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — диетолог-эксперт и нутрициолог. Твоя задача — анализировать блюда и продукты питания, которые описывает пользователь, и рассчитывать их пищевую ценность.

Для каждого блюда или продукта предоставляй:
- Калорийность (ккал)
- Белки (г)
- Жиры (г)
- Углеводы (г)
- Клетчатка (г)

Формат ответа:
🍽️ **[Название блюда]**
├─ Калории: X ккал
├─ Белки: X г
├─ Жиры: X г
├─ Углеводы: X г
└─ Клетчатка: X г

📝 [Краткий комментарий о блюде, если нужно]

Если пользователь описывает порцию (например, "100г гречки" или "тарелка борща"), рассчитывай для указанного количества. Если порция не указана, рассчитывай на стандартную порцию и уточни это.

Если пользователь указывает несколько блюд или ингредиентов, рассчитывай каждое отдельно, а затем обязательно выведи итоговую сумму в следующем формате:

📊 **ИТОГО:**
├─ Калории: X ккал
├─ Белки: X г
├─ Жиры: X г
├─ Углеводы: X г
└─ Клетчатка: X г

Всегда отвечай на русском языке. Будь дружелюбным и полезным."""

user_histories: dict[int, list] = {}
pending_nutrition: dict[int, dict] = {}

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


# ─── Парсинг ответа Клода ────────────────────────────────────────────────────

def parse_nutrition_from_response(text: str) -> dict | None:
    dish_re  = re.compile(r"🍽️\s*\*{0,2}([^\*\n]+?)\*{0,2}\s*\n")
    cal_re   = re.compile(r"Калории[:\s]+(\d+(?:[.,]\d+)?)\s*ккал", re.IGNORECASE)
    prot_re  = re.compile(r"Белки[:\s]+(\d+(?:[.,]\d+)?)\s*г",      re.IGNORECASE)
    fat_re   = re.compile(r"Жиры[:\s]+(\d+(?:[.,]\d+)?)\s*г",       re.IGNORECASE)
    carb_re  = re.compile(r"Углеводы[:\s]+(\d+(?:[.,]\d+)?)\s*г",   re.IGNORECASE)
    fiber_re = re.compile(r"Клетчатка[:\s]+(\d+(?:[.,]\d+)?)\s*г",  re.IGNORECASE)
    itogo_re = re.compile(r"(?:итого|итог)[^\n]*\n", re.IGNORECASE)

    def to_float(v):
        return float(v.replace(",", "."))

    def first_float(pattern, source):
        m = pattern.search(source)
        return to_float(m.group(1)) if m else None

    def floats(p, source):
        return [to_float(v) for v in p.findall(source)]

    dishes = dish_re.findall(text)

    # Ищем блок ИТОГО и берём значения оттуда
    itogo_match = itogo_re.search(text)
    if itogo_match:
        itogo_text = text[itogo_match.start():]
        cal  = first_float(cal_re,  itogo_text)
        prot = first_float(prot_re, itogo_text)
        fat  = first_float(fat_re,  itogo_text)
        carb = first_float(carb_re, itogo_text)
        fib  = first_float(fiber_re, itogo_text)
        if cal is not None:
            dish = ("Несколько блюд: " + ", ".join(dishes)) if dishes else "Несколько блюд"
            return {
                "dish":     dish.strip(),
                "calories": round(cal,  1),
                "proteins": round(prot or 0, 1),
                "fats":     round(fat  or 0, 1),
                "carbs":    round(carb or 0, 1),
                "fiber":    round(fib  or 0, 1),
            }

    # Нет блока ИТОГО — одно блюдо или сумма всех значений
    calories_all = floats(cal_re,  text)
    proteins_all = floats(prot_re, text)
    fats_all     = floats(fat_re,  text)
    carbs_all    = floats(carb_re, text)
    fiber_all    = floats(fiber_re, text)

    if not calories_all:
        return None

    dish = ", ".join(dishes) if dishes else "Блюдо"
    return {
        "dish":     dish.strip(),
        "calories": round(sum(calories_all), 1),
        "proteins": round(sum(proteins_all), 1),
        "fats":     round(sum(fats_all),     1),
        "carbs":    round(sum(carbs_all),    1),
        "fiber":    round(sum(fiber_all),    1),
    }


# ─── Форматирование ──────────────────────────────────────────────────────────

def fmt_total(total: dict, header: str = "") -> str:
    return (
        f"{header}\n\n" if header else ""
    ) + (
        f"├─ Калории: {total['calories']} ккал\n"
        f"├─ Белки: {total['proteins']} г\n"
        f"├─ Жиры: {total['fats']} г\n"
        f"├─ Углеводы: {total['carbs']} г\n"
        f"└─ Клетчатка: {total['fiber']} г"
    )


# ─── Команды ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_histories[user_id] = []
    db_ensure_user(user_id)
    await update.message.reply_text(
        "Привет! 👋 Я помогу тебе следить за питанием — просто опиши что съел, а я посчитаю калории и БЖУ.\n\n"
        "*Как это работает:*\n"
        "Напиши что ты съел — можно коротко или с деталями:\n"
        "— \"гречка с курицей\"\n"
        "— \"гречка 150г с куриной грудкой 200г\"\n"
        "— \"большая пицца пепперони, два куска\"\n"
        "— \"кофе с молоком 50мл и ложкой сахара\"\n\n"
        "Чем точнее опишешь граммовку — тем точнее будет расчёт. Но если не знаешь — я сам прикину стандартную порцию 🙂\n\n"
        "*Что умею:*\n"
        "💾 После каждого расчёта можешь сохранить еду в дневник — просто нажми кнопку\n"
        "📊 /stats — сколько уже съел сегодня\n"
        "📅 /history — твои записи за последние 7 дней\n\n"
        "Каждый день в 23:55 пришлю итог за день 🌙",
        parse_mode="Markdown",
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_histories[update.effective_user.id] = []
    await update.message.reply_text("История диалога очищена. Начинаем заново! 🔄")


async def cleartoday_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db_ensure_user(user_id)

    user = db_get_user(user_id)
    user_tz = timezone(timedelta(hours=user["timezone_offset"] if user else 0))
    today = datetime.now(user_tz).date()

    total = db_get_day_total(user_id, today)
    if total is None or total["count"] == 0:
        await update.message.reply_text("За сегодня записей в дневнике нет.")
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data=f"cleartoday_confirm:{today}"),
        InlineKeyboardButton("❌ Отмена",      callback_data="cleartoday_cancel"),
    ]])
    await update.message.reply_text(
        f"Вы уверены? Будут удалены *{total['count']} записей* за сегодня "
        f"({total['calories']} ккал).",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def handle_cleartoday_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if query.data == "cleartoday_cancel":
        await query.edit_message_text("Удаление отменено.")
        return

    # callback_data: "cleartoday_confirm:YYYY-MM-DD"
    _, date_str = query.data.split(":", 1)
    entry_date = date.fromisoformat(date_str)

    deleted = db_delete_today_entries(user_id, entry_date)
    await query.edit_message_text(
        f"Удалено записей: {deleted}. Дневник за {date_str} очищен."
    )


async def timezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db_ensure_user(user_id)

    if not context.args:
        user = db_get_user(user_id)
        offset = user["timezone_offset"] if user else 0
        sign = "+" if offset >= 0 else ""
        await update.message.reply_text(
            f"🕐 Ваш текущий часовой пояс: *UTC{sign}{offset}*\n\n"
            "Чтобы изменить, напишите, например:\n"
            "`/timezone +4` — Тбилиси / Баку\n"
            "`/timezone +3` — Москва\n"
            "`/timezone 0`  — UTC",
            parse_mode="Markdown",
        )
        return

    arg = context.args[0].strip().lstrip("+")
    try:
        offset = int(arg)
        if not -12 <= offset <= 14:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат. Используйте: `/timezone +4` (от -12 до +14)",
            parse_mode="Markdown",
        )
        return

    db_set_timezone(user_id, offset)
    sign = "+" if offset >= 0 else ""
    await update.message.reply_text(
        f"✅ Часовой пояс установлен: *UTC{sign}{offset}*\n"
        "Ежедневный итог будет приходить в 23:55 по вашему времени.",
        parse_mode="Markdown",
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db_ensure_user(user_id)

    user = db_get_user(user_id)
    user_tz = timezone(timedelta(hours=user["timezone_offset"]))
    today = datetime.now(user_tz).date()

    entries = db_get_day_entries(user_id, today)
    if not entries:
        await update.message.reply_text("📓 Сегодня ещё ничего не сохранено.")
        return

    total = db_get_day_total(user_id, today)
    lines = [f"📓 *Дневник питания за {today}*\n"]

    for e in entries:
        lines.append(
            f"🍽️ *{e['dish']}* ({e['time']})\n"
            f"  Кал: {e['calories']} | Б: {e['proteins']}г | "
            f"Ж: {e['fats']}г | У: {e['carbs']}г | Кл: {e['fiber']}г\n"
        )

    lines.append("─────────────────────")
    lines.append(fmt_total(total, f"📊 *Итог за {today}*"))

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db_ensure_user(user_id)

    user = db_get_user(user_id)
    user_tz = timezone(timedelta(hours=user["timezone_offset"]))
    today = datetime.now(user_tz).date()
    week_ago = today - timedelta(days=6)

    rows = db_get_history(user_id, week_ago, today)
    if not rows:
        await update.message.reply_text("📓 За последние 7 дней записей нет.")
        return

    lines = ["📅 *История питания за 7 дней*\n"]
    for r in rows:
        label = "сегодня" if r["date"] == today else r["date"].strftime("%d.%m")
        lines.append(
            f"📆 *{label}* ({r['date']}) — {r['count']} зап.\n"
            f"  Кал: *{r['calories']}* ккал | "
            f"Б: {r['proteins']}г | Ж: {r['fats']}г | "
            f"У: {r['carbs']}г | Кл: {r['fiber']}г\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── Основной обработчик сообщений ──────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if user_id not in user_histories:
        user_histories[user_id] = []

    user_histories[user_id].append({"role": "user", "content": update.message.text})

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        response = anthropic_client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=user_histories[user_id],
        )
        reply = response.content[0].text
        user_histories[user_id].append({"role": "assistant", "content": reply})

        nutrition = parse_nutrition_from_response(reply)

        if nutrition:
            pending_nutrition[user_id] = nutrition
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("💾 Сохранить в дневник", callback_data="save_diary")]]
            )
            await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await update.message.reply_text(reply, parse_mode="Markdown")

    except anthropic.AuthenticationError:
        await update.message.reply_text("❌ Ошибка: неверный API ключ Anthropic.")
    except anthropic.RateLimitError:
        await update.message.reply_text("⏳ Превышен лимит запросов. Попробуйте чуть позже.")
    except anthropic.APIConnectionError:
        await update.message.reply_text("🌐 Нет подключения к API. Проверьте интернет.")
    except anthropic.APIStatusError as e:
        await update.message.reply_text(f"⚠️ Ошибка API ({e.status_code}): {e.message}")
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        await update.message.reply_text("❌ Произошла непредвиденная ошибка.")


# ─── Callback: сохранение в дневник ─────────────────────────────────────────

async def handle_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id

    await query.answer()

    if user_id not in pending_nutrition:
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="❌ Данные уже сохранены или устарели.",
        )
        return

    nutrition = pending_nutrition.pop(user_id)

    db_ensure_user(user_id)
    user = db_get_user(user_id)
    user_tz = timezone(timedelta(hours=user["timezone_offset"]))
    now = datetime.now(user_tz)
    dish = nutrition.pop("dish")

    db_add_entry(user_id, now.date(), now.strftime("%H:%M"), dish, nutrition)

    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(
            f"✅ *Сохранено в дневник!*\n\n"
            f"🍽️ {dish}\n"
            f"├─ Калории: {nutrition['calories']} ккал\n"
            f"├─ Белки: {nutrition['proteins']} г\n"
            f"├─ Жиры: {nutrition['fats']} г\n"
            f"├─ Углеводы: {nutrition['carbs']} г\n"
            f"└─ Клетчатка: {nutrition['fiber']} г\n\n"
            f"Итог за день: /stats"
        ),
        parse_mode="Markdown",
    )


# ─── Ежедневная рассылка (каждую минуту, мульти-тайм-зона) ──────────────────

async def check_and_send_summaries(context: ContextTypes.DEFAULT_TYPE) -> None:
    now_utc = datetime.now(timezone.utc)

    for user in db_get_all_users():
        user_tz   = timezone(timedelta(hours=user["timezone_offset"]))
        now_local = now_utc.astimezone(user_tz)

        if not (now_local.hour == 23 and now_local.minute == 55):
            continue

        today = now_local.date()

        if user["last_summary_sent"] and user["last_summary_sent"] >= today:
            continue

        total = db_get_day_total(user["user_id"], today)
        db_mark_summary_sent(user["user_id"], today)

        if total is None:
            continue  # ничего не сохранено — не отправляем

        text = (
            f"🌙 *Итог питания за {today}*\n"
            f"_({total['count']} записей)_\n\n"
            + fmt_total(total)
        )

        try:
            await context.bot.send_message(
                chat_id=user["user_id"], text=text, parse_mode="Markdown"
            )
            logger.info("Итог отправлен пользователю %s за %s.", user["user_id"], today)
        except Exception as e:
            logger.warning("Не удалось отправить итог %s: %s", user["user_id"], e)


# ─── Запуск ──────────────────────────────────────────────────────────────────

def main() -> None:
    for var in ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "DATABASE_URL"):
        if not os.environ.get(var):
            raise SystemExit(f"Ошибка: переменная окружения {var} не установлена.")

    init_db()

    app = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()

    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("reset",      reset))
    app.add_handler(CommandHandler("timezone",   timezone_command))
    app.add_handler(CommandHandler("stats",      stats_command))
    app.add_handler(CommandHandler("history",    history_command))
    app.add_handler(CommandHandler("cleartoday", cleartoday_command))
    app.add_handler(CallbackQueryHandler(handle_save_callback,      pattern="^save_diary$"))
    app.add_handler(CallbackQueryHandler(handle_cleartoday_callback, pattern="^cleartoday_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Каждую минуту проверяем у кого 23:55 по местному времени
    app.job_queue.run_repeating(check_and_send_summaries, interval=60, first=10)

    logger.info("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()
