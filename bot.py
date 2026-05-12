"""
Telegram-бот для автопубликации новостей об ИИ.
Запускается по расписанию через GitHub Actions:
парсит RSS → Claude выбирает лучшую новость → переписывает её цепляюще → постит.
"""

import os
import json
import logging
import hashlib
import asyncio
from pathlib import Path

import feedparser
from anthropic import Anthropic
from telegram import Bot
from telegram.constants import ParseMode

# ============================================================
# НАСТРОЙКИ
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# RSS-источники новостей про ИИ
RSS_SOURCES = [
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("MIT Technology Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    ("Ars Technica AI", "https://arstechnica.com/ai/feed/"),
    ("Хабр ИИ", "https://habr.com/ru/rss/hub/artificial_intelligence/all/?fl=ru"),
    ("Хабр Машинное обучение", "https://habr.com/ru/rss/hub/machine_learning/all/?fl=ru"),
]

# Сколько новостей показать Claude для выбора лучшей
NEWS_POOL_SIZE = 20

POSTED_FILE = Path("posted_news.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ============================================================
# ХРАНЕНИЕ ОПУБЛИКОВАННЫХ НОВОСТЕЙ
# ============================================================

def load_posted_ids() -> set:
    if POSTED_FILE.exists():
        try:
            with open(POSTED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("ids", []))
        except Exception as e:
            logger.warning(f"Не удалось загрузить файл истории: {e}")
    return set()


def save_posted_id(news_id: str) -> None:
    ids = load_posted_ids()
    ids.add(news_id)
    ids_list = list(ids)[-1000:]
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump({"ids": ids_list}, f, ensure_ascii=False, indent=2)
    logger.info(f"💾 Сохранено в историю: {news_id}")


def make_news_id(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:16]


# ============================================================
# ПАРСИНГ RSS
# ============================================================

def fetch_news() -> list[dict]:
    posted_ids = load_posted_ids()
    all_news = []

    for source_name, rss_url in RSS_SOURCES:
        try:
            logger.info(f"Парсим: {source_name}")
            feed = feedparser.parse(rss_url)

            for entry in feed.entries[:10]:
                url = entry.get("link", "")
                if not url:
                    continue

                news_id = make_news_id(url)
                if news_id in posted_ids:
                    continue

                summary = entry.get("summary", "") or entry.get("description", "")
                summary = (
                    summary.replace("<p>", "")
                    .replace("</p>", "\n")
                    .replace("<br>", "\n")
                    .replace("<br/>", "\n")
                )

                all_news.append({
                    "id": news_id,
                    "title": entry.get("title", "").strip(),
                    "summary": summary.strip()[:1500],
                    "url": url,
                    "source": source_name,
                    "published": entry.get("published", ""),
                })
        except Exception as e:
            logger.error(f"Ошибка при парсинге {source_name}: {e}")

    logger.info(f"📰 Всего свежих новостей: {len(all_news)}")
    return all_news


# ============================================================
# ВЫБОР ЛУЧШЕЙ НОВОСТИ ЧЕРЕЗ CLAUDE
# ============================================================

SELECTOR_PROMPT = """Ты — главный редактор крутого Telegram-канала про ИИ. Твоя задача — из списка новостей выбрать ОДНУ самую интересную для русскоязычной аудитории.

КРИТЕРИИ ОЦЕНКИ (по важности):
1. Реальное событие, а не маркетинговая хрень. Запуски моделей > обновления функций > слухи > мнения.
2. Влияние на индустрию или жизнь людей. Прорывы > улучшения > мелочи.
3. Уникальность темы. Новые имена/исследования > 100500-я статья про ChatGPT.
4. Практическая польза или яркий wow-эффект.

ИЗБЕГАЙ:
- Чисто корпоративных новостей (поглощения, инвестиции без продукта) — кроме реально крупных
- Дублирующих тем, которые недавно муссировались всеми
- Длинных аналитических лонгридов без явного инфоповода

Тебе на вход даётся список новостей в формате:
[номер] (источник) Заголовок
Краткое описание...

ОТВЕТЬ ТОЛЬКО ОДНОЙ ЦИФРОЙ — номером самой интересной новости. Без объяснений, без текста, без точек. Просто число."""


def select_best_news(news_list: list[dict]) -> dict | None:
    """Claude выбирает самую интересную новость из списка."""
    if not news_list:
        return None

    # Если новость только одна — без выбора
    if len(news_list) == 1:
        logger.info("📌 Новость одна, выбор не нужен")
        return news_list[0]

    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)

        # Формируем список для Claude
        candidates_text = "\n\n".join([
            f"[{i + 1}] ({news['source']}) {news['title']}\n"
            f"{news['summary'][:400]}"
            for i, news in enumerate(news_list)
        ])

        logger.info(f"🤔 Claude выбирает лучшую из {len(news_list)} новостей...")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=SELECTOR_PROMPT,
            messages=[{"role": "user", "content": candidates_text}],
        )

        answer = response.content[0].text.strip()
        # Достаём число из ответа (на случай если Claude добавил лишнее)
        digits = "".join(c for c in answer if c.isdigit())
        if not digits:
            logger.warning(f"⚠️ Claude вернул некорректный выбор: '{answer}', беру первую")
            return news_list[0]

        index = int(digits) - 1
        if index < 0 or index >= len(news_list):
            logger.warning(f"⚠️ Claude вернул выход за диапазон: {index + 1}, беру первую")
            return news_list[0]

        chosen = news_list[index]
        logger.info(f"✅ Выбрана #{index + 1}: {chosen['title'][:60]}...")
        return chosen

    except Exception as e:
        logger.error(f"❌ Ошибка при выборе новости: {e}, беру первую")
        return news_list[0]


# ============================================================
# НАПИСАНИЕ ПОСТА ЧЕРЕЗ CLAUDE
# ============================================================

WRITER_PROMPT = """Ты — топовый автор Telegram-канала про ИИ. Твои посты лайкают и пересылают. Твоя задача — превратить новость в КОРОТКИЙ цепляющий пост.

ФОРМАТ ПОСТА (строго в таком порядке):

1. КРЮК — первая строка. Это вопрос, интригующая фраза или яркое утверждение. Цель — заставить остановить скролл. БЕЗ эмодзи в начале.

2. <b>Заголовок жирным</b> — короткий, не длиннее 8 слов. Без эмодзи.

3. СУТЬ — 2-3 коротких предложения. ЧТО произошло. Без воды, без преамбул.

4. ПОЧЕМУ ЭТО ВАЖНО — 1-2 предложения. Зачем читателю это знать. Что это меняет. Какие будут последствия.

5. ФИНАЛЬНАЯ ФРАЗА — короткая мысль, провокация или вопрос. Чтобы хотелось поставить эмодзи или написать в комменты.

ЖЁСТКИЕ ОГРАНИЧЕНИЯ:
- ВЕСЬ пост — максимум 500 символов. Чем короче, тем лучше.
- Не больше 3 эмодзи во всём посте. Они для акцентов, а не для украшения.
- Короткие предложения. Если можешь сократить — сократи.
- Пустые строки между блоками для воздуха.

ЗАПРЕЩЕНО:
❌ "В современном мире..."
❌ "Стоит отметить, что..."
❌ "Важно понимать..."
❌ "Эксперты считают..."
❌ "Не за горами тот день..."
❌ Канцелярит. Сложные слова. Длинные предложения.
❌ Пересказ всей статьи. Тебе нужна СУТЬ.

ХОРОШИЕ КРЮКИ (примеры):
✅ "OpenAI снова сделала это."
✅ "Кажется, программистам пора нервничать."
✅ "Это изменит то, как мы общаемся с ИИ."
✅ "Думаешь, такое возможно? Уже да."

ПЛОХИЕ КРЮКИ:
❌ "Сегодня мы расскажем..."
❌ "В мире искусственного интеллекта произошло важное событие..."
❌ "Компания X объявила о выпуске..."

HTML-ТЕГИ (только эти):
<b>жирный</b> — для заголовка и редких акцентов
<i>курсив</i> — для редких выделений
НЕ используй другие теги.

Не добавляй "Источник:", "Ссылка:", хэштеги — это всё добавится автоматически.
Пиши на русском, всегда, даже если исходник на английском."""


def rewrite_news_with_claude(news: dict) -> str | None:
    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        user_message = f"""Напиши цепляющий короткий пост по этой новости:

Заголовок: {news['title']}

Содержание: {news['summary']}

Источник: {news['source']}"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            system=WRITER_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        text = response.content[0].text.strip()
        logger.info(f"✏️ Пост написан, длина: {len(text)} символов")
        return text

    except Exception as e:
        logger.error(f"❌ Ошибка Claude API: {e}")
        return None


# ============================================================
# ПУБЛИКАЦИЯ В TELEGRAM
# ============================================================

async def publish_to_telegram(text: str, source_url: str, source_name: str) -> bool:
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        full_text = (
            f"{text}\n\n"
            f"🔗 <a href=\"{source_url}\">Источник: {source_name}</a>\n\n"
            f"#ИИ #AI #новости"
        )

        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=full_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        logger.info("✅ Пост опубликован в канал")
        return True

    except Exception as e:
        logger.error(f"❌ Ошибка публикации: {e}")
        return False


# ============================================================
# ОСНОВНАЯ ЛОГИКА
# ============================================================

async def main() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHANNEL_ID:
        missing.append("TELEGRAM_CHANNEL_ID")
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        logger.error(f"❌ Не заданы секреты: {', '.join(missing)}")
        raise SystemExit(1)

    logger.info("🚀 Начинаю цикл публикации")

    # 1. Парсим RSS
    news_list = fetch_news()
    if not news_list:
        logger.warning("⚠️ Нет свежих новостей")
        return

    # 2. Оставляем топ-N для выбора (свежие в начале)
    candidates = news_list[:NEWS_POOL_SIZE]
    logger.info(f"🎯 Кандидатов на публикацию: {len(candidates)}")

    # 3. Claude выбирает лучшую
    best = select_best_news(candidates)
    if not best:
        logger.warning("⚠️ Не удалось выбрать новость")
        return

    # 4. Claude пишет пост
    post_text = rewrite_news_with_claude(best)
    if not post_text:
        logger.error("❌ Не удалось написать пост")
        return

    # 5. Публикуем
    success = await publish_to_telegram(
        text=post_text,
        source_url=best["url"],
        source_name=best["source"],
    )

    if success:
        save_posted_id(best["id"])
        logger.info(f"🎉 Готово: {best['title'][:60]}...")


if __name__ == "__main__":
    asyncio.run(main())
