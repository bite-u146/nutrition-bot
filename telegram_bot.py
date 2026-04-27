import os
import io
import logging
import re
import traceback
import zipfile
import anthropic
import psycopg2
import psycopg2.pool
from contextlib import contextmanager
from datetime import datetime, date, time as dtime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ConversationHandler,
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

# ─── In-memory cache ──────────────────────────────────────────────────────────
# user_id -> {timezone_offset, last_summary_sent, calorie_goal}
_user_cache: dict[int, dict] = {}
# user_id -> {date, entries, total}
_stats_cache: dict[int, dict] = {}

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id           BIGINT PRIMARY KEY,
    timezone_offset   INTEGER NOT NULL DEFAULT 0,
    last_summary_sent DATE,
    last_weekly_sent  DATE
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS last_weekly_sent DATE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS calorie_goal INTEGER;
ALTER TABLE users ADD COLUMN IF NOT EXISTS gender TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS age INTEGER;
ALTER TABLE users ADD COLUMN IF NOT EXISTS height_cm REAL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS weight_kg REAL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS goal_type TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS activity_level TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS streak_current INTEGER NOT NULL DEFAULT 0;

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

CREATE TABLE IF NOT EXISTS conversation_history (
    id         SERIAL PRIMARY KEY,
    user_id    BIGINT  NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conv_history_user ON conversation_history(user_id, id);

CREATE TABLE IF NOT EXISTS favorites (
    id         SERIAL PRIMARY KEY,
    user_id    BIGINT  NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    dish       TEXT    NOT NULL,
    calories   REAL    NOT NULL DEFAULT 0,
    proteins   REAL    NOT NULL DEFAULT 0,
    fats       REAL    NOT NULL DEFAULT 0,
    carbs      REAL    NOT NULL DEFAULT 0,
    fiber      REAL    NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id);
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
                "SELECT timezone_offset, last_summary_sent, calorie_goal FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {"timezone_offset": row[0], "last_summary_sent": row[1], "calorie_goal": row[2]}


def db_set_timezone(user_id: int, offset: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET timezone_offset = %s WHERE user_id = %s",
                (offset, user_id),
            )
        conn.commit()
    if user_id in _user_cache:
        _user_cache[user_id]["timezone_offset"] = offset


def db_set_calorie_goal(user_id: int, goal: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET calorie_goal = %s WHERE user_id = %s",
                (goal, user_id),
            )
        conn.commit()
    if user_id in _user_cache:
        _user_cache[user_id]["calorie_goal"] = goal


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
    _stats_cache.pop(user_id, None)
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
    _stats_cache.pop(user_id, None)


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
            cur.execute(
                "SELECT user_id, timezone_offset, last_summary_sent, last_weekly_sent, "
                "calorie_goal, goal_type, streak_current FROM users"
            )
            rows = cur.fetchall()
    return [
        {
            "user_id": r[0],
            "timezone_offset": r[1],
            "last_summary_sent": r[2],
            "last_weekly_sent": r[3],
            "calorie_goal": r[4],
            "goal_type": r[5],
            "streak_current": r[6],
        }
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


def db_mark_weekly_sent(user_id: int, week_monday: date) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET last_weekly_sent = %s WHERE user_id = %s",
                (week_monday, user_id),
            )
        conn.commit()


def db_save_profile(
    user_id: int,
    gender: str,
    age: int,
    height_cm: float,
    weight_kg: float,
    goal_type: str,
    activity_level: str,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET gender=%s, age=%s, height_cm=%s, weight_kg=%s,
                    goal_type=%s, activity_level=%s
                WHERE user_id=%s
                """,
                (gender, age, height_cm, weight_kg, goal_type, activity_level, user_id),
            )
        conn.commit()


def db_get_profile(user_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT gender, age, height_cm, weight_kg, goal_type, activity_level
                FROM users WHERE user_id=%s
                """,
                (user_id,),
            )
            row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return {
        "gender":         row[0],
        "age":            row[1],
        "height_cm":      row[2],
        "weight_kg":      row[3],
        "goal_type":      row[4],
        "activity_level": row[5],
    }


def db_get_streak(user_id: int) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT streak_current FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
    return row[0] if row else 0


def db_update_streak(user_id: int, streak: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET streak_current = %s WHERE user_id = %s",
                (streak, user_id),
            )
        conn.commit()


def db_append_message(user_id: int, role: str, content: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversation_history (user_id, role, content) VALUES (%s, %s, %s)",
                (user_id, role, content),
            )
        conn.commit()


def db_get_conversation(user_id: int, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT role, content FROM (
                    SELECT id, role, content
                    FROM conversation_history
                    WHERE user_id = %s
                    ORDER BY id DESC
                    LIMIT %s
                ) sub ORDER BY id ASC
                """,
                (user_id, limit),
            )
            rows = cur.fetchall()
    return [{"role": r[0], "content": r[1]} for r in rows]


def db_clear_conversation(user_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM conversation_history WHERE user_id = %s",
                (user_id,),
            )
        conn.commit()


def db_add_favorite(user_id: int, name: str, dish: str, nutrition: dict) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO favorites (user_id, name, dish, calories, proteins, fats, carbs, fiber)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, name) DO UPDATE SET
                    dish=EXCLUDED.dish, calories=EXCLUDED.calories,
                    proteins=EXCLUDED.proteins, fats=EXCLUDED.fats,
                    carbs=EXCLUDED.carbs, fiber=EXCLUDED.fiber,
                    created_at=NOW()
                """,
                (user_id, name, dish,
                 nutrition["calories"], nutrition["proteins"],
                 nutrition["fats"], nutrition["carbs"], nutrition["fiber"]),
            )
        conn.commit()


def db_get_favorites(user_id: int) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, dish, calories, proteins, fats, carbs, fiber
                FROM favorites WHERE user_id = %s ORDER BY name
                """,
                (user_id,),
            )
            rows = cur.fetchall()
    return [
        {
            "id": r[0], "name": r[1], "dish": r[2],
            "calories": r[3], "proteins": r[4],
            "fats": r[5], "carbs": r[6], "fiber": r[7],
        }
        for r in rows
    ]


def db_get_favorite(user_id: int, fav_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, dish, calories, proteins, fats, carbs, fiber
                FROM favorites WHERE user_id = %s AND id = %s
                """,
                (user_id, fav_id),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "name": row[1], "dish": row[2],
        "calories": row[3], "proteins": row[4],
        "fats": row[5], "carbs": row[6], "fiber": row[7],
    }


def db_delete_favorite(user_id: int, fav_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM favorites WHERE user_id = %s AND id = %s",
                (user_id, fav_id),
            )
            deleted = cur.rowcount
        conn.commit()
    return deleted > 0


def check_goal_met(calories: float, goal: int, goal_type: str) -> bool:
    """Проверяет, выполнена ли цель по калориям в зависимости от типа цели."""
    if goal_type == "lose":
        return calories <= goal
    if goal_type == "maintain":
        return goal * 0.9 <= calories <= goal * 1.1
    if goal_type == "gain":
        return calories >= goal
    return False


# ─── Кэширующие обёртки ──────────────────────────────────────────────────────

def get_user_cached(user_id: int) -> dict | None:
    if user_id not in _user_cache:
        user = db_get_user(user_id)
        if user is not None:
            _user_cache[user_id] = user
    return _user_cache.get(user_id)


def get_stats_cached(user_id: int, today: date) -> dict:
    cached = _stats_cache.get(user_id)
    if cached and cached["date"] == today:
        return cached
    entries = db_get_day_entries(user_id, today)
    total = db_get_day_total(user_id, today)
    result = {"date": today, "entries": entries, "total": total}
    _stats_cache[user_id] = result
    return result


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

СТРОГО ЗАПРЕЩЕНО после блока 📝 Комментарий (или после блока 📊 ИТОГО при нескольких блюдах) добавлять что-либо ещё: никаких сводок за день, суммарных данных по всем приёмам пищи за день, итогов за день, анализа питания, рекомендаций или любых других дополнений. Твой ответ должен заканчиваться сразу после 📝 Комментария (или после блока 📊 ИТОГО). Статистика за день показывается только по команде /stats — не дублируй её здесь.

Всегда отвечай на русском языке. Будь дружелюбным и полезным."""

pending_nutrition: dict[int, dict] = {}
last_nutrition: dict[int, dict] = {}

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


# ─── Парсинг ответа Клода ────────────────────────────────────────────────────

def parse_nutrition_from_response(text: str) -> dict | None:
    dish_re  = re.compile(r"🍽️[^\n]*?\*{1,2}([^*\n]+)\*{1,2}")
    cal_re   = re.compile(r"Калори[а-яё]*[:\s]+(\d+(?:[.,]\d+)?)\s*ккал", re.IGNORECASE)
    prot_re  = re.compile(r"Белки[:\s]+(\d+(?:[.,]\d+)?)\s*г",           re.IGNORECASE)
    fat_re   = re.compile(r"Жиры[:\s]+(\d+(?:[.,]\d+)?)\s*г",            re.IGNORECASE)
    carb_re  = re.compile(r"Углеводы[:\s]+(\d+(?:[.,]\d+)?)\s*г",        re.IGNORECASE)
    fiber_re = re.compile(r"Клетчатка[:\s]+(\d+(?:[.,]\d+)?)\s*г",       re.IGNORECASE)
    itogo_re = re.compile(r"(?:итого|итог)\b[^\n]*\n", re.IGNORECASE)

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


def fmt_goal_progress(current: float, goal: int) -> str:
    current_int = round(current)
    percent = round(current / goal * 100)
    filled = min(10, round(current / goal * 10))
    bar = "▓" * filled + "░" * (10 - filled)
    if current_int > goal:
        status = f"🔴 Цель превышена на {current_int - goal} ккал"
    else:
        status = f"{current_int} / {goal} ккал — осталось {goal - current_int} ккал"
    return f"🎯 Цель: {goal} ккал\n{bar} {percent}%\n{status}"


# ─── Команды ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db_ensure_user(user_id)
    db_clear_conversation(user_id)
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
        "💾 После каждого расчёта можешь сохранить еду в дневник — просто нажми кнопку\n\n"
        "Каждый день в 23:55 пришлю итог за день 🌙\n\n"
        "📋 *Доступные команды:*\n\n"
        "/stats — итог питания за сегодня\n"
        "/favorites — избранные блюда\n"
        "/week — еженедельный отчёт\n"
        "/profile — настроить личный профиль и узнать рекомендуемую норму калорий\n"
        "/goal — установить ежедневную цель по калориям\n"
        "/cleartoday — удалить все записи за сегодня\n"
        "/timezone — посмотреть / установить часовой пояс",
        parse_mode="Markdown",
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db_ensure_user(user_id)
    db_clear_conversation(user_id)
    await update.message.reply_text("История диалога очищена. Начинаем заново! 🔄")


async def cleartoday_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db_ensure_user(user_id)

    user = get_user_cached(user_id)
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
        user = get_user_cached(user_id)
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


async def goal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db_ensure_user(user_id)

    if not context.args:
        user = get_user_cached(user_id)
        goal = user["calorie_goal"] if user else None
        if goal:
            await update.message.reply_text(
                f"🎯 Твоя текущая цель: *{goal} ккал/день*\n\n"
                "Чтобы изменить: `/goal 2000`",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "🎯 Цель по калориям не установлена.\n\n"
                "Установи командой, например: `/goal 2000`",
                parse_mode="Markdown",
            )
        return

    try:
        goal = int(context.args[0])
        if not 100 <= goal <= 10000:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Укажи число от 100 до 10000. Например: `/goal 2000`",
            parse_mode="Markdown",
        )
        return

    db_set_calorie_goal(user_id, goal)
    await update.message.reply_text(
        f"✅ Цель установлена: *{goal} ккал/день*\n"
        "Прогресс будет отображаться после каждого сохранения и в /stats",
        parse_mode="Markdown",
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db_ensure_user(user_id)

    user = get_user_cached(user_id)
    user_tz = timezone(timedelta(hours=user["timezone_offset"]))
    today = datetime.now(user_tz).date()

    stats = get_stats_cached(user_id, today)
    entries = stats["entries"]
    if not entries:
        await update.message.reply_text("📓 Сегодня ещё ничего не сохранено.")
        return

    total = stats["total"]
    lines = [f"📓 *Дневник питания за {today}*\n"]

    for e in entries:
        lines.append(
            f"🍽️ *{e['dish']}* ({e['time']})\n"
            f"  Кал: {e['calories']} | Б: {e['proteins']}г | "
            f"Ж: {e['fats']}г | У: {e['carbs']}г | Кл: {e['fiber']}г\n"
        )

    lines.append("─────────────────────")
    lines.append(fmt_total(total, f"📊 *Итог за {today}*"))

    goal = user["calorie_goal"]
    if goal:
        lines.append("\n" + fmt_goal_progress(total["calories"], goal))
    else:
        lines.append("\n_Установи цель командой /goal чтобы отслеживать прогресс_")

    streak = db_get_streak(user_id)
    if streak > 0:
        lines.append(f"\n🔥 Streak: {streak} дней подряд")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db_ensure_user(user_id)

    user = get_user_cached(user_id)
    user_tz = timezone(timedelta(hours=user["timezone_offset"]))
    today = datetime.now(user_tz).date()

    # Понедельник текущей недели
    monday = today - timedelta(days=today.weekday())

    rows = db_get_history(user_id, monday, today)
    by_date = {r["date"]: r for r in rows}

    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    lines = ["📅 *Отчёт за неделю:*\n"]
    total_calories = 0
    days_with_entries = 0

    current = monday
    while current <= today:
        day_name = day_names[current.weekday()]
        day_str = current.strftime("%d.%m")
        if current in by_date:
            cal = round(by_date[current]["calories"])
            lines.append(f"{day_name} {day_str} — {cal} ккал")
            total_calories += cal
            days_with_entries += 1
        else:
            lines.append(f"{day_name} {day_str} — 0 ккал (нет записей)")
        current += timedelta(days=1)

    if days_with_entries > 0:
        avg = round(total_calories / days_with_entries)
        lines.append(f"\n🔥 Среднее за неделю: {avg} ккал")
    else:
        lines.append("\n_(за эту неделю записей нет)_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── Профиль пользователя (/profile) ─────────────────────────────────────────

PROFILE_GENDER, PROFILE_AGE, PROFILE_HEIGHT, PROFILE_WEIGHT, PROFILE_GOAL, PROFILE_ACTIVITY = range(6)

GOAL_LABELS      = {"lose": "Похудение", "maintain": "Поддержание", "gain": "Набор массы"}
ACTIVITY_LABELS  = {"low": "Низкий", "medium": "Средний", "high": "Высокий"}
ACTIVITY_MULT    = {"low": 1.2, "medium": 1.375, "high": 1.55}
GOAL_MULT        = {"lose": 0.85, "maintain": 1.0, "gain": 1.15}


def calculate_calories(gender: str, age: int, height_cm: float, weight_kg: float,
                        activity_level: str, goal_type: str) -> int:
    if gender == "male":
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    else:
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age - 161
    return round(bmr * ACTIVITY_MULT[activity_level] * GOAL_MULT[goal_type])


async def profile_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    db_ensure_user(user_id)
    context.user_data["profile"] = {}

    existing = db_get_profile(user_id)
    intro = ""
    if existing:
        gender_label   = "Мужской" if existing["gender"] == "male" else "Женский"
        goal_label     = GOAL_LABELS.get(existing["goal_type"], "—")
        activity_label = ACTIVITY_LABELS.get(existing["activity_level"], "—")
        intro = (
            f"📋 *Текущий профиль:*\n"
            f"Пол: {gender_label} | Возраст: {existing['age']} лет\n"
            f"Рост: {existing['height_cm']} см | Вес: {existing['weight_kg']} кг\n"
            f"Цель: {goal_label} | Активность: {activity_label}\n\n"
        )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("👨 Мужской", callback_data="pg:male"),
        InlineKeyboardButton("👩 Женский", callback_data="pg:female"),
    ]])
    await update.message.reply_text(
        intro + "Шаг 1/6 — *Укажи свой пол:*",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return PROFILE_GENDER


async def profile_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    gender = query.data.split(":")[1]
    context.user_data["profile"]["gender"] = gender
    label = "👨 Мужской" if gender == "male" else "👩 Женский"
    await query.edit_message_text(f"Шаг 1/6 — Пол: *{label}*", parse_mode="Markdown")
    await query.message.reply_text("Шаг 2/6 — *Сколько тебе лет?* (введи число)")
    return PROFILE_AGE


async def profile_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        age = int(update.message.text.strip())
        if not 10 <= age <= 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введи корректный возраст от 10 до 100.")
        return PROFILE_AGE
    context.user_data["profile"]["age"] = age
    await update.message.reply_text("Шаг 3/6 — *Рост в сантиметрах?* (введи число)")
    return PROFILE_HEIGHT


async def profile_height(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        height = float(update.message.text.strip().replace(",", "."))
        if not 100 <= height <= 250:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введи корректный рост от 100 до 250 см.")
        return PROFILE_HEIGHT
    context.user_data["profile"]["height_cm"] = height
    await update.message.reply_text("Шаг 4/6 — *Вес в килограммах?* (введи число)")
    return PROFILE_WEIGHT


async def profile_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        weight = float(update.message.text.strip().replace(",", "."))
        if not 30 <= weight <= 300:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введи корректный вес от 30 до 300 кг.")
        return PROFILE_WEIGHT
    context.user_data["profile"]["weight_kg"] = weight
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📉 Похудение",      callback_data="pgl:lose")],
        [InlineKeyboardButton("⚖️ Поддержание",    callback_data="pgl:maintain")],
        [InlineKeyboardButton("📈 Набор массы",    callback_data="pgl:gain")],
    ])
    await update.message.reply_text(
        "Шаг 5/6 — *Какова твоя цель?*",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return PROFILE_GOAL


async def profile_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    goal = query.data.split(":")[1]
    context.user_data["profile"]["goal_type"] = goal
    await query.edit_message_text(
        f"Шаг 5/6 — Цель: *{GOAL_LABELS[goal]}*", parse_mode="Markdown"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🪑 Низкий — сидячий образ жизни",       callback_data="pa:low")],
        [InlineKeyboardButton("🏃 Средний — тренировки 2-3 раза/нед",  callback_data="pa:medium")],
        [InlineKeyboardButton("💪 Высокий — тренировки 5+ раз/нед",    callback_data="pa:high")],
    ])
    await query.message.reply_text(
        "Шаг 6/6 — *Уровень физической активности:*",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return PROFILE_ACTIVITY


async def profile_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    activity = query.data.split(":")[1]
    await query.edit_message_text(
        f"Шаг 6/6 — Активность: *{ACTIVITY_LABELS[activity]}*", parse_mode="Markdown"
    )

    p = context.user_data.pop("profile")
    p["activity_level"] = activity
    user_id = query.from_user.id

    db_save_profile(
        user_id, p["gender"], p["age"], p["height_cm"],
        p["weight_kg"], p["goal_type"], p["activity_level"],
    )

    kcal = calculate_calories(
        p["gender"], p["age"], p["height_cm"],
        p["weight_kg"], p["activity_level"], p["goal_type"],
    )

    gender_label   = "👨 Мужской" if p["gender"] == "male" else "👩 Женский"
    goal_label     = GOAL_LABELS[p["goal_type"]]
    activity_label = ACTIVITY_LABELS[p["activity_level"]]

    await query.message.reply_text(
        f"👤 *Твой профиль сохранён!*\n\n"
        f"Пол: {gender_label} | Возраст: {p['age']} лет\n"
        f"Рост: {p['height_cm']} см | Вес: {p['weight_kg']} кг\n"
        f"Цель: {goal_label} | Активность: {activity_label}\n\n"
        f"📊 *На основе твоих данных:*\n"
        f"🔥 Рекомендуемая норма калорий: *{kcal} ккал/день*\n\n"
        f"💡 Хочешь установить это как цель? Введи `/goal {kcal}`",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def profile_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("profile", None)
    await update.message.reply_text("Заполнение профиля отменено.")
    return ConversationHandler.END


# ─── Основной обработчик сообщений ──────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db_ensure_user(user_id)

    # Перехватываем ввод названия для избранного
    if context.user_data.get("awaiting_fav_name"):
        context.user_data.pop("awaiting_fav_name")
        nutrition = context.user_data.pop("pending_fav_nutrition", None)
        name = update.message.text.strip()
        if nutrition and name:
            db_add_favorite(user_id, name, nutrition.get("dish", name), nutrition)
            await update.message.reply_text(
                f"⭐ *«{name}»* добавлено в избранное!\n\n"
                f"├─ Калории: {nutrition['calories']} ккал\n"
                f"├─ Белки: {nutrition['proteins']} г\n"
                f"├─ Жиры: {nutrition['fats']} г\n"
                f"├─ Углеводы: {nutrition['carbs']} г\n"
                f"└─ Клетчатка: {nutrition['fiber']} г",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text("❌ Ошибка при сохранении. Попробуй снова.")
        return

    db_append_message(user_id, "user", update.message.text)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        messages = db_get_conversation(user_id)
        response = anthropic_client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        reply = response.content[0].text
        db_append_message(user_id, "assistant", reply)

        nutrition = parse_nutrition_from_response(reply)
        logger.info("Parsed nutrition for user %s: %s", user_id, nutrition)

        if nutrition:
            last_nutrition[user_id] = dict(nutrition)
            pending_nutrition[user_id] = dict(nutrition)
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("💾 Сохранить в дневник", callback_data="save_diary")]]
            )
            try:
                await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=keyboard)
            except Exception:
                await update.message.reply_text(reply, reply_markup=keyboard)
        else:
            try:
                await update.message.reply_text(reply, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(reply)

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
    user = get_user_cached(user_id)
    user_tz = timezone(timedelta(hours=user["timezone_offset"]))
    now = datetime.now(user_tz)
    dish = nutrition.pop("dish")

    db_add_entry(user_id, now.date(), now.strftime("%H:%M"), dish, nutrition)

    total = get_stats_cached(user_id, now.date())["total"]
    goal = user["calorie_goal"]

    if goal and total:
        progress = "\n\n" + fmt_goal_progress(total["calories"], goal)
    elif total:
        progress = "\n\n_Установи цель командой /goal чтобы отслеживать прогресс_"
    else:
        progress = ""

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
            f"└─ Клетчатка: {nutrition['fiber']} г"
            f"{progress}"
        ),
        parse_mode="Markdown",
    )


# ─── Избранные блюда (/favorites) ────────────────────────────────────────────

def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Мои избранные", callback_data="fav:list"),
        InlineKeyboardButton("➕ Добавить",      callback_data="fav:add_start"),
        InlineKeyboardButton("🗑️ Удалить",      callback_data="fav:delete_mode"),
    ]])


def _menu_text(has_favorites: bool) -> str:
    if has_favorites:
        return "⭐ *Избранные блюда*"
    return "У тебя пока нет избранных блюд. Нажми ➕ Добавить чтобы сохранить первое!"


def _delete_list_keyboard(favorites: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            f"❌ {fav['name']} ({round(fav['calories'])} ккал)",
            callback_data=f"fav:del_ask:{fav['id']}",
        )]
        for fav in favorites
    ]
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="fav:menu")])
    return InlineKeyboardMarkup(rows)


async def favorites_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db_ensure_user(user_id)
    favorites = db_get_favorites(user_id)
    await update.message.reply_text(
        _menu_text(bool(favorites)),
        parse_mode="Markdown",
        reply_markup=_menu_keyboard(),
    )


async def handle_favorites_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    parts = query.data.split(":")
    action = parts[1]
    fav_id = int(parts[2]) if len(parts) > 2 else None

    if action == "menu":
        favorites = db_get_favorites(user_id)
        await query.edit_message_text(
            _menu_text(bool(favorites)),
            parse_mode="Markdown",
            reply_markup=_menu_keyboard(),
        )

    elif action == "list":
        favorites = db_get_favorites(user_id)
        if not favorites:
            await query.edit_message_text(
                _menu_text(False), reply_markup=_menu_keyboard()
            )
            return
        rows = [
            [InlineKeyboardButton(
                f"🍽️ {fav['name']} ({round(fav['calories'])} ккал)",
                callback_data=f"fav:show:{fav['id']}",
            )]
            for fav in favorites
        ]
        rows.append([InlineKeyboardButton("🔙 Назад", callback_data="fav:menu")])
        await query.edit_message_text(
            "⭐ *Мои избранные блюда:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    elif action == "show":
        fav = db_get_favorite(user_id, fav_id)
        if not fav:
            await query.edit_message_text("❌ Блюдо не найдено.", reply_markup=_menu_keyboard())
            return
        nutrition_copy = {
            "dish": fav["dish"],
            "calories": fav["calories"],
            "proteins": fav["proteins"],
            "fats": fav["fats"],
            "carbs": fav["carbs"],
            "fiber": fav["fiber"],
        }
        pending_nutrition[user_id] = dict(nutrition_copy)
        last_nutrition[user_id] = dict(nutrition_copy)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("💾 Сохранить в дневник", callback_data="save_diary"),
            InlineKeyboardButton("🔙 Список",              callback_data="fav:list"),
        ]])
        await query.edit_message_text(
            f"⭐ *{fav['name']}*\n\n"
            f"🍽️ {fav['dish']}\n"
            f"├─ Калории: {fav['calories']} ккал\n"
            f"├─ Белки: {fav['proteins']} г\n"
            f"├─ Жиры: {fav['fats']} г\n"
            f"├─ Углеводы: {fav['carbs']} г\n"
            f"└─ Клетчатка: {fav['fiber']} г",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    elif action == "add_start":
        nutrition = last_nutrition.get(user_id)
        if not nutrition:
            await query.edit_message_text(
                "❌ Нет блюда для сохранения.\n\n"
                "Сначала отправь мне описание блюда — я рассчитаю калории, "
                "а потом сможешь добавить его в избранное.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Назад", callback_data="fav:menu")
                ]]),
            )
            return
        context.user_data["awaiting_fav_name"] = True
        context.user_data["pending_fav_nutrition"] = dict(nutrition)
        await query.edit_message_text(
            f"💾 *Сохраняю в избранное:*\n\n"
            f"🍽️ {nutrition['dish']}\n"
            f"├─ Калории: {nutrition['calories']} ккал\n"
            f"├─ Белки: {nutrition['proteins']} г\n"
            f"├─ Жиры: {nutrition['fats']} г\n"
            f"├─ Углеводы: {nutrition['carbs']} г\n"
            f"└─ Клетчатка: {nutrition['fiber']} г\n\n"
            f"Введи название для этого блюда:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="fav:add_cancel")
            ]]),
        )

    elif action == "add_cancel":
        context.user_data.pop("awaiting_fav_name", None)
        context.user_data.pop("pending_fav_nutrition", None)
        favorites = db_get_favorites(user_id)
        await query.edit_message_text(
            _menu_text(bool(favorites)),
            parse_mode="Markdown",
            reply_markup=_menu_keyboard(),
        )

    elif action == "delete_mode":
        favorites = db_get_favorites(user_id)
        if not favorites:
            await query.edit_message_text(
                _menu_text(False), reply_markup=_menu_keyboard()
            )
            return
        await query.edit_message_text(
            "🗑️ *Выбери блюдо для удаления:*",
            parse_mode="Markdown",
            reply_markup=_delete_list_keyboard(favorites),
        )

    elif action == "del_ask":
        fav = db_get_favorite(user_id, fav_id)
        if not fav:
            await query.edit_message_text("❌ Блюдо не найдено.", reply_markup=_menu_keyboard())
            return
        await query.edit_message_text(
            f"Удалить *«{fav['name']}»* из избранного?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, удалить", callback_data=f"fav:del_confirm:{fav_id}"),
                InlineKeyboardButton("🔙 Назад",       callback_data="fav:delete_mode"),
            ]]),
        )

    elif action == "del_confirm":
        fav = db_get_favorite(user_id, fav_id)
        name = fav["name"] if fav else "блюдо"
        db_delete_favorite(user_id, fav_id)
        favorites = db_get_favorites(user_id)
        if not favorites:
            await query.edit_message_text(
                f"✅ *{name}* удалено.\n\n{_menu_text(False)}",
                parse_mode="Markdown",
                reply_markup=_menu_keyboard(),
            )
            return
        await query.edit_message_text(
            f"✅ *{name}* удалено.\n\n🗑️ *Выбери блюдо для удаления:*",
            parse_mode="Markdown",
            reply_markup=_delete_list_keyboard(favorites),
        )


# ─── Рассылка (каждую минуту, мульти-тайм-зона) ─────────────────────────────

async def check_and_send_summaries(context: ContextTypes.DEFAULT_TYPE) -> None:
    now_utc = datetime.now(timezone.utc)

    for user in db_get_all_users():
        user_tz   = timezone(timedelta(hours=user["timezone_offset"]))
        now_local = now_utc.astimezone(user_tz)

        if not (now_local.hour == 23 and now_local.minute == 55):
            continue

        today = now_local.date()
        uid   = user["user_id"]

        # ── Ежедневный итог ──────────────────────────────────────────────────
        if not (user["last_summary_sent"] and user["last_summary_sent"] >= today):
            total = db_get_day_total(uid, today)
            db_mark_summary_sent(uid, today)

            # Обновляем streak
            calorie_goal = user.get("calorie_goal")
            goal_type    = user.get("goal_type")
            new_streak   = 0
            if calorie_goal and goal_type:
                calories_today = total["calories"] if total else 0.0
                if check_goal_met(calories_today, calorie_goal, goal_type):
                    new_streak = user["streak_current"] + 1
                db_update_streak(uid, new_streak)

            if total is not None:
                text = (
                    f"🌙 *Итог питания за {today}*\n"
                    f"_({total['count']} записей)_\n\n"
                    + fmt_total(total)
                )
                if new_streak > 0:
                    text += f"\n\n🔥 Streak: {new_streak} дней подряд"
                try:
                    await context.bot.send_message(
                        chat_id=uid, text=text, parse_mode="Markdown"
                    )
                    logger.info("Дневной итог отправлен пользователю %s за %s.", uid, today)
                except Exception as e:
                    logger.warning("Не удалось отправить дневной итог %s: %s", uid, e)

        # ── Еженедельный отчёт (только воскресенье) ──────────────────────────
        if now_local.weekday() != 6:  # 6 = воскресенье
            continue

        monday = today - timedelta(days=6)

        if user["last_weekly_sent"] and user["last_weekly_sent"] >= monday:
            continue

        db_mark_weekly_sent(uid, monday)

        rows = db_get_history(uid, monday, today)
        if not rows:
            continue  # за всю неделю нет записей — не отправляем

        by_date = {r["date"]: r for r in rows}
        day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        lines = ["📅 *Отчёт за неделю:*\n"]
        total_calories = 0
        days_with_entries = 0

        current = monday
        while current <= today:
            day_name = day_names[current.weekday()]
            day_str  = current.strftime("%d.%m")
            if current in by_date:
                cal = round(by_date[current]["calories"])
                lines.append(f"{day_name} {day_str} — {cal} ккал")
                total_calories += cal
                days_with_entries += 1
            else:
                lines.append(f"{day_name} {day_str} — 0 ккал (нет записей)")
            current += timedelta(days=1)

        avg = round(total_calories / days_with_entries)
        lines.append(f"\n🔥 Среднее за неделю: {avg} ккал")

        try:
            await context.bot.send_message(
                chat_id=uid, text="\n".join(lines), parse_mode="Markdown"
            )
            logger.info("Еженедельный отчёт отправлен пользователю %s (неделя с %s).", uid, monday)
        except Exception as e:
            logger.warning("Не удалось отправить еженедельный отчёт %s: %s", uid, e)


# ─── Обработчик необработанных ошибок ────────────────────────────────────────

ADMIN_ID = 587184112


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    tb = "".join(traceback.format_exception(
        type(context.error), context.error, context.error.__traceback__
    ))
    logger.error("Необработанная ошибка:\n%s", tb)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    text = (
        f"🚨 *Ошибка в боте:*\n\n"
        f"`{tb[-3000:]}`\n\n"
        f"🕐 Время: {now}"
    )
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID, text=text, parse_mode="Markdown"
        )
    except Exception as e:
        logger.error("Не удалось отправить уведомление об ошибке: %s", e)


# ─── Резервное копирование ───────────────────────────────────────────────────

def generate_backup() -> bytes:
    """Экспортирует все таблицы в CSV через COPY TO STDOUT и упаковывает в zip."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("backup_info.txt", f"Backup generated at {now_str}\n")
        with get_conn() as conn:
            with conn.cursor() as cur:
                for table in ("users", "diary_entries"):
                    table_buf = io.BytesIO()
                    cur.copy_expert(
                        f"COPY {table} TO STDOUT WITH CSV HEADER", table_buf
                    )
                    zf.writestr(f"{table}.csv", table_buf.getvalue().decode("utf-8"))
    buf.seek(0)
    return buf.read()


async def weekly_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(timezone.utc)
    logger.info("Запуск еженедельного бэкапа БД...")
    try:
        data = generate_backup()
        filename = f"backup_{now.strftime('%Y-%m-%d')}.zip"
        await context.bot.send_document(
            chat_id=ADMIN_ID,
            document=io.BytesIO(data),
            filename=filename,
            caption=f"💾 Еженедельный бэкап базы данных {now.strftime('%d.%m.%Y')}",
        )
        logger.info("Бэкап успешно отправлен (%d байт).", len(data))
    except Exception as e:
        logger.error("Ошибка при создании бэкапа: %s", e)
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"🚨 Ошибка при создании бэкапа:\n`{e}`",
                parse_mode="Markdown",
            )
        except Exception:
            pass


# ─── Запуск ──────────────────────────────────────────────────────────────────

async def post_init(app) -> None:
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("stats",      "📊 Статистика за сегодня"),
        BotCommand("favorites",  "⭐ Избранные блюда"),
        BotCommand("profile",    "👤 Мой профиль и норма калорий"),
        BotCommand("goal",       "🎯 Установить цель по калориям"),
        BotCommand("week",       "📅 Отчёт за текущую неделю"),
        BotCommand("cleartoday", "🗑 Удалить записи за сегодня"),
        BotCommand("timezone",   "🕐 Настроить часовой пояс"),
        BotCommand("reset",      "🔄 Очистить историю диалога"),
    ])


def main() -> None:
    for var in ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "DATABASE_URL"):
        if not os.environ.get(var):
            raise SystemExit(f"Ошибка: переменная окружения {var} не установлена.")

    init_db()

    app = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).post_init(post_init).build()

    profile_conv = ConversationHandler(
        entry_points=[CommandHandler("profile", profile_start)],
        states={
            PROFILE_GENDER:   [CallbackQueryHandler(profile_gender,   pattern="^pg:")],
            PROFILE_AGE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, profile_age)],
            PROFILE_HEIGHT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, profile_height)],
            PROFILE_WEIGHT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, profile_weight)],
            PROFILE_GOAL:     [CallbackQueryHandler(profile_goal,     pattern="^pgl:")],
            PROFILE_ACTIVITY: [CallbackQueryHandler(profile_activity, pattern="^pa:")],
        },
        fallbacks=[CommandHandler("cancel", profile_cancel)],
        allow_reentry=True,
    )
    app.add_handler(profile_conv)

    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("reset",      reset))
    app.add_handler(CommandHandler("timezone",   timezone_command))
    app.add_handler(CommandHandler("goal",       goal_command))
    app.add_handler(CommandHandler("stats",      stats_command))
    app.add_handler(CommandHandler("week",       week_command))
    app.add_handler(CommandHandler("cleartoday", cleartoday_command))
    app.add_handler(CommandHandler("favorites",  favorites_command))
    app.add_handler(CallbackQueryHandler(handle_save_callback,       pattern="^save_diary$"))
    app.add_handler(CallbackQueryHandler(handle_cleartoday_callback, pattern="^cleartoday_"))
    app.add_handler(CallbackQueryHandler(handle_favorites_callback,  pattern="^fav:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    # Каждую минуту проверяем у кого 23:55 по местному времени
    app.job_queue.run_repeating(check_and_send_summaries, interval=60, first=10)

    # Бэкап БД каждое воскресенье в 03:00 UTC (days: 0=вс, 1=пн, ..., 6=сб по PTB)
    app.job_queue.run_daily(weekly_backup, time=dtime(3, 0, tzinfo=timezone.utc), days=(0,))

    logger.info("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()
