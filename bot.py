"""
Telegram-бот для автопубликации новостей об ИИ.
Запускается по расписанию через GitHub Actions:
парсит RSS → обрабатывает через Claude → постит одну новость → завершается.
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
    # Англоязычные топ-источники
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("MIT Technology Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    ("Ars Technica AI", "https://arstechnica.com/ai/feed/"),
    # Русскоязычные
    ("Хабр ИИ", "https://habr.com/ru/rss/hub/artificial_intelligence/all/?fl=ru"),
    ("Хабр Машинное обучение", "https://habr.com/ru/rss/hub/machine_learning/all/?fl=ru"),
]

# Файл с историей опубликованных новостей (хранится в репозитории)
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
    # Храним только последние 1000 ID
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
# ОБРАБОТКА ЧЕРЕЗ CLAUDE
# ============================================================

SYSTEM_PROMPT = """Ты — редактор популярного Telegram-канала про искусственный интеллект на русском языке.

Твоя задача — переписать новость или статью так, чтобы она цепляла читателя и была написана живым языком. Никакого канцелярита и сухих официальных формулировок.

ПРАВИЛА ОФОРМЛЕНИЯ:
1. Цепляющий заголовок жирным шрифтом (используй <b>текст</b>) — без эмодзи в начале
2. Основной текст — 3-5 коротких абзацев, разделённых пустой строкой
3. Используй 2-4 эмодзи по тексту, но не перебарщивай
4. В конце — короткий вывод или провокационный вопрос для обсуждения
5. Максимум 800 символов в основном тексте (Telegram-формат)
6. Пиши на русском языке, даже если исходник на английском

СТИЛЬ:
- Простые слова, без жаргона
- Объясняй технические термины
- Если новость про продукт — пиши, зачем это нужно простому человеку
- Если про исследование — объясняй практическую пользу
- Никаких "В современном мире...", "Стоит отметить...", "Важно понимать..."

ФОРМАТ HTML (Telegram поддерживает только эти теги):
- <b>жирный</b>
- <i>курсив</i>
- <code>код</code>
- НЕ используй другие HTML-теги

Не добавляй ссылку на источник — она будет добавлена автоматически.
Не добавляй хэштеги — они будут добавлены автоматически.
Не пиши "Новость:" или "Источник:" — сразу с заголовка.
"""


def rewrite_news_with_claude(news: dict) -> str | None:
    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        user_message = f"""Перепиши эту новость для Telegram-канала про ИИ:

Заголовок: {news['title']}

Содержание: {news['summary']}

Источник: {news['source']}"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        text = response.content[0].text.strip()
        logger.info(f"✏️ Claude обработал, длина: {len(text)} символов")
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
    # Проверка ключей
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

    news_list = fetch_news()
    if not news_list:
        logger.warning("⚠️ Нет свежих новостей для публикации")
        return

    # Берём первую необработанную новость
    for news in news_list:
        rewritten = rewrite_news_with_claude(news)
        if not rewritten:
            continue

        success = await publish_to_telegram(
            text=rewritten,
            source_url=news["url"],
            source_name=news["source"],
        )

        if success:
            save_posted_id(news["id"])
            logger.info(f"✅ Опубликовано: {news['title'][:60]}...")
            return

    logger.warning("❌ Не удалось опубликовать ни одной новости")


if __name__ == "__main__":
    asyncio.run(main())
