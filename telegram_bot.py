import os
import logging
import anthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

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

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_histories[user_id] = []
    await update.message.reply_text(
        "🥗 *Бот для расчёта калорий и питательной ценности блюд*\n\n"
        "Опишите блюдо или продукт — я рассчитаю его пищевую ценность.\n\n"
        "Например: _«тарелка борща»_, _«100г куриной грудки»_, _«банан»_\n\n"
        "/reset — очистить историю диалога",
        parse_mode="Markdown",
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_histories[user_id] = []
    await update.message.reply_text("История диалога очищена. Начинаем заново! 🔄")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_text = update.message.text

    if user_id not in user_histories:
        user_histories[user_id] = []

    user_histories[user_id].append({"role": "user", "content": user_text})

    # Показываем "печатает..."
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен. Ожидаю сообщений...")
    app.run_polling()


if __name__ == "__main__":
    main()
