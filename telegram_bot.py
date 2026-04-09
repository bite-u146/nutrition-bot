import os
import json
import logging
import re
import anthropic
from datetime import datetime, time, timezone, timedelta
from pathlib import Path
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

DIARY_FILE = Path("diary.json")

# Структура diary.json:
# {
#   "<user_id>": {
#     "timezone_offset": 4,          # смещение UTC в часах
#     "last_summary_sent": "2026-04-09",
#     "2026-04-09": {
#       "entries": [{"time": "12:30", "dish": "...", "calories": 0, ...}],
#       "total":   {"calories": 0, "proteins": 0, "fats": 0, "carbs": 0, "fiber": 0}
#     }
#   }
# }

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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

Если пользователь указывает несколько блюд или ингредиентов, рассчитывай каждое отдельно и давай итоговую сумму.

Всегда отвечай на русском языке. Будь дружелюбным и полезным."""

# История диалогов: {user_id: [{"role": ..., "content": ...}, ...]}
user_histories: dict[int, list] = {}

# Последние распарсенные данные о питании, ожидающие сохранения
pending_nutrition: dict[int, dict] = {}

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


# ─── Дневник (JSON) ──────────────────────────────────────────────────────────

def load_diary() -> dict:
    if DIARY_FILE.exists():
        with open(DIARY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_diary_file(data: dict) -> None:
    with open(DIARY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user_tz(user_data: dict) -> timezone:
    offset = user_data.get("timezone_offset", 0)
    return timezone(timedelta(hours=offset))


def ensure_user(diary: dict, uid: str) -> None:
    """Создаёт запись пользователя если её нет."""
    if uid not in diary:
        diary[uid] = {"timezone_offset": 0}


def add_diary_entry(
    user_id: int, date_str: str, time_str: str, dish: str, nutrition: dict
) -> None:
    diary = load_diary()
    uid = str(user_id)
    ensure_user(diary, uid)

    if date_str not in diary[uid]:
        diary[uid][date_str] = {
            "entries": [],
            "total": {"calories": 0, "proteins": 0, "fats": 0, "carbs": 0, "fiber": 0},
        }

    diary[uid][date_str]["entries"].append({"time": time_str, "dish": dish, **nutrition})

    for key in ("calories", "proteins", "fats", "carbs", "fiber"):
        diary[uid][date_str]["total"][key] = round(
            diary[uid][date_str]["total"].get(key, 0) + nutrition.get(key, 0), 1
        )

    save_diary_file(diary)


# ─── Парсинг ответа Клода ────────────────────────────────────────────────────

def parse_nutrition_from_response(text: str) -> dict | None:
    """
    Извлекает КБЖУ из ответа Клода.
    Если блюд несколько — берёт последний блок значений (итог).
    """
    dish_re = re.compile(r"🍽️\s*\*{0,2}([^\*\n]+?)\*{0,2}\s*\n")
    cal_re = re.compile(r"Калории[:\s]+(\d+(?:[.,]\d+)?)\s*ккал", re.IGNORECASE)
    prot_re = re.compile(r"Белки[:\s]+(\d+(?:[.,]\d+)?)\s*г", re.IGNORECASE)
    fat_re = re.compile(r"Жиры[:\s]+(\d+(?:[.,]\d+)?)\s*г", re.IGNORECASE)
    carb_re = re.compile(r"Углеводы[:\s]+(\d+(?:[.,]\d+)?)\s*г", re.IGNORECASE)
    fiber_re = re.compile(r"Клетчатка[:\s]+(\d+(?:[.,]\d+)?)\s*г", re.IGNORECASE)

    def floats(pattern):
        return [float(v.replace(",", ".")) for v in pattern.findall(text)]

    dishes = dish_re.findall(text)
    calories_all = floats(cal_re)
    proteins_all = floats(prot_re)
    fats_all = floats(fat_re)
    carbs_all = floats(carb_re)
    fiber_all = floats(fiber_re)

    if not calories_all:
        return None

    # Если наборов значений больше чем блюд — последний набор это итог
    if len(calories_all) > max(len(dishes), 1):
        calories = calories_all[-1]
        proteins = proteins_all[-1] if proteins_all else 0
        fats = fats_all[-1] if fats_all else 0
        carbs = carbs_all[-1] if carbs_all else 0
        fiber = fiber_all[-1] if fiber_all else 0
        dish = "Несколько блюд: " + ", ".join(dishes) if dishes else "Несколько блюд"
    else:
        calories = sum(calories_all)
        proteins = sum(proteins_all) if proteins_all else 0
        fats = sum(fats_all) if fats_all else 0
        carbs = sum(carbs_all) if carbs_all else 0
        fiber = sum(fiber_all) if fiber_all else 0
        dish = ", ".join(dishes) if dishes else "Блюдо"

    return {
        "dish": dish.strip(),
        "calories": round(calories, 1),
        "proteins": round(proteins, 1),
        "fats": round(fats, 1),
        "carbs": round(carbs, 1),
        "fiber": round(fiber, 1),
    }


# ─── Форматирование ──────────────────────────────────────────────────────────

def format_daily_summary(date_str: str, total: dict, n_entries: int = 0) -> str:
    header = f"📊 *Итог за {date_str}*"
    if n_entries:
        header += f" _({n_entries} зап.)_"
    return (
        f"{header}\n\n"
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

    # Регистрируем пользователя если новый
    diary = load_diary()
    uid = str(user_id)
    if uid not in diary:
        ensure_user(diary, uid)
        save_diary_file(diary)

    await update.message.reply_text(
        "🥗 *Бот для расчёта калорий и питательной ценности блюд*\n\n"
        "Опишите блюдо или продукт — я рассчитаю его пищевую ценность.\n\n"
        "Например: _«тарелка борща»_, _«100г куриной грудки»_, _«банан»_\n\n"
        "📋 *Команды:*\n"
        "/stats — итог питания за сегодня\n"
        "/history — записи за последние 7 дней\n"
        "/timezone — установить часовой пояс\n"
        "/reset — очистить историю диалога",
        parse_mode="Markdown",
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_histories[user_id] = []
    await update.message.reply_text("История диалога очищена. Начинаем заново! 🔄")


async def timezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /timezone        — показать текущий часовой пояс
    /timezone +4     — установить UTC+4
    /timezone -5     — установить UTC-5
    """
    user_id = update.effective_user.id
    uid = str(user_id)
    diary = load_diary()
    ensure_user(diary, uid)

    if not context.args:
        offset = diary[uid].get("timezone_offset", 0)
        sign = "+" if offset >= 0 else ""
        await update.message.reply_text(
            f"🕐 Ваш текущий часовой пояс: *UTC{sign}{offset}*\n\n"
            "Чтобы изменить, напишите, например:\n"
            "`/timezone +4` для Тбилиси/Баку\n"
            "`/timezone +3` для Москвы\n"
            "`/timezone 0`  для UTC",
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
            "❌ Неверный формат. Используйте: `/timezone +4` (значения от -12 до +14)",
            parse_mode="Markdown",
        )
        return

    diary[uid]["timezone_offset"] = offset
    save_diary_file(diary)

    sign = "+" if offset >= 0 else ""
    await update.message.reply_text(
        f"✅ Часовой пояс установлен: *UTC{sign}{offset}*\n"
        "Ежедневный итог будет приходить в 23:55 по вашему времени.",
        parse_mode="Markdown",
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Итог за сегодня с подробными записями."""
    user_id = update.effective_user.id
    uid = str(user_id)
    diary = load_diary()

    if uid not in diary:
        await update.message.reply_text("📓 Дневник питания пуст. Сохраните первое блюдо!")
        return

    user_tz = get_user_tz(diary[uid])
    date_str = datetime.now(user_tz).strftime("%Y-%m-%d")

    if date_str not in diary[uid] or not diary[uid][date_str].get("entries"):
        await update.message.reply_text("📓 Сегодня ещё ничего не сохранено.")
        return

    day = diary[uid][date_str]
    lines = [f"📓 *Дневник питания за {date_str}*\n"]

    for entry in day["entries"]:
        lines.append(
            f"🍽️ *{entry['dish']}* ({entry['time']})\n"
            f"  Кал: {entry['calories']} | Б: {entry['proteins']}г | "
            f"Ж: {entry['fats']}г | У: {entry['carbs']}г | Кл: {entry['fiber']}г\n"
        )

    lines.append("─────────────────────")
    lines.append(format_daily_summary(date_str, day["total"]))

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Итог по дням за последние 7 дней."""
    user_id = update.effective_user.id
    uid = str(user_id)
    diary = load_diary()

    if uid not in diary:
        await update.message.reply_text("📓 История питания пуста.")
        return

    user_tz = get_user_tz(diary[uid])
    today = datetime.now(user_tz)

    lines = ["📅 *История питания за 7 дней*\n"]
    found_any = False

    for i in range(7):
        day_dt = today - timedelta(days=i)
        date_str = day_dt.strftime("%Y-%m-%d")

        if date_str not in diary[uid] or not diary[uid][date_str].get("entries"):
            continue

        found_any = True
        day = diary[uid][date_str]
        total = day["total"]
        n = len(day["entries"])
        label = "сегодня" if i == 0 else day_dt.strftime("%d.%m")

        lines.append(
            f"📆 *{label}* ({date_str}) — {n} зап.\n"
            f"  Кал: *{total['calories']}* ккал | "
            f"Б: {total['proteins']}г | Ж: {total['fats']}г | "
            f"У: {total['carbs']}г | Кл: {total['fiber']}г\n"
        )

    if not found_any:
        await update.message.reply_text("📓 За последние 7 дней записей нет.")
        return

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── Основной обработчик сообщений ──────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_text = update.message.text

    if user_id not in user_histories:
        user_histories[user_id] = []

    user_histories[user_id].append({"role": "user", "content": user_text})

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
            await update.message.reply_text(
                reply, parse_mode="Markdown", reply_markup=keyboard
            )
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
    uid = str(user_id)

    await query.answer()

    if user_id not in pending_nutrition:
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="❌ Данные уже сохранены или устарели.",
        )
        return

    nutrition = pending_nutrition.pop(user_id)

    # Используем часовой пояс пользователя для записи времени
    diary = load_diary()
    ensure_user(diary, uid)
    user_tz = get_user_tz(diary[uid])
    now = datetime.now(user_tz)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    dish = nutrition.pop("dish")

    add_diary_entry(user_id, date_str, time_str, dish, nutrition)

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


# ─── Ежедневный итог (каждую минуту проверяем все часовые пояса) ─────────────

async def check_and_send_summaries(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Запускается каждую минуту.
    Для каждого пользователя проверяет: если у него сейчас 23:55 — отправляет итог.
    Флаг last_summary_sent предотвращает повторную отправку.
    """
    now_utc = datetime.now(timezone.utc)
    diary = load_diary()
    changed = False

    for uid_str, user_data in diary.items():
        if not isinstance(user_data, dict):
            continue

        user_tz = get_user_tz(user_data)
        now_local = now_utc.astimezone(user_tz)

        # Проверяем что сейчас 23:55 по местному времени
        if not (now_local.hour == 23 and now_local.minute == 55):
            continue

        date_str = now_local.strftime("%Y-%m-%d")

        # Уже отправляли сегодня?
        if user_data.get("last_summary_sent") == date_str:
            continue

        # Есть ли записи за сегодня?
        if date_str not in user_data or not user_data[date_str].get("entries"):
            # Ничего не сохранено — не отправляем, но ставим флаг чтобы не проверять снова
            diary[uid_str]["last_summary_sent"] = date_str
            changed = True
            continue

        total = user_data[date_str]["total"]
        n = len(user_data[date_str]["entries"])

        text = (
            f"🌙 *Итог питания за {date_str}*\n"
            f"_({n} записей)_\n\n"
            + format_daily_summary(date_str, total)
        )

        try:
            await context.bot.send_message(
                chat_id=int(uid_str),
                text=text,
                parse_mode="Markdown",
            )
            logger.info("Ежедневный итог отправлен пользователю %s за %s.", uid_str, date_str)
        except Exception as e:
            logger.warning("Не удалось отправить итог пользователю %s: %s", uid_str, e)

        diary[uid_str]["last_summary_sent"] = date_str
        changed = True

    if changed:
        save_diary_file(diary)


# ─── Запуск ──────────────────────────────────────────────────────────────────

def main() -> None:
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not telegram_token:
        raise SystemExit("Ошибка: переменная окружения TELEGRAM_BOT_TOKEN не установлена.")

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        raise SystemExit("Ошибка: переменная окружения ANTHROPIC_API_KEY не установлена.")

    app = ApplicationBuilder().token(telegram_token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("timezone", timezone_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CallbackQueryHandler(handle_save_callback, pattern="^save_diary$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Каждую минуту проверяем: у кого сейчас 23:55 по местному времени
    app.job_queue.run_repeating(check_and_send_summaries, interval=60, first=10)

    logger.info("Бот запущен. Ожидаю сообщений...")
    app.run_polling()


if __name__ == "__main__":
    main()
