import os
import sys
import anthropic

# Принудительно UTF-8 для вывода в терминале Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

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


def run_bot():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Ошибка: переменная окружения ANTHROPIC_API_KEY не установлена.")
        return

    client = anthropic.Anthropic(api_key=api_key)
    conversation_history = []

    print("=" * 60)
    print("🥗 Бот для расчёта калорий и питательной ценности блюд")
    print("=" * 60)
    print("Опишите блюдо или продукт, и я рассчитаю его пищевую ценность.")
    print("Для выхода введите 'выход', 'exit' или нажмите Ctrl+C.")
    print("=" * 60)
    print()

    while True:
        try:
            user_input = input("Вы: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\nДо свидания! Питайтесь правильно! 🥦")
            break

        if not user_input:
            continue

        if user_input.lower() in ("выход", "exit", "quit", "q"):
            print("До свидания! Питайтесь правильно! 🥦")
            break

        conversation_history.append({"role": "user", "content": user_input})

        try:
            response = client.messages.create(
                model="claude-opus-4-6",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=conversation_history,
            )

            assistant_message = response.content[0].text
            conversation_history.append(
                {"role": "assistant", "content": assistant_message}
            )

            print(f"\nБот: {assistant_message}\n")

        except anthropic.AuthenticationError:
            print("Ошибка: неверный API ключ. Проверьте ANTHROPIC_API_KEY.")
            break
        except anthropic.RateLimitError:
            print("Ошибка: превышен лимит запросов. Попробуйте позже.")
        except anthropic.APIConnectionError:
            print("Ошибка: нет подключения к API. Проверьте интернет-соединение.")
        except anthropic.APIStatusError as e:
            print(f"Ошибка API ({e.status_code}): {e.message}")


if __name__ == "__main__":
    run_bot()
