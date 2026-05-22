"""
Telegram-бот для AI-канала.
Каждый день недели — своя рубрика.
Пн-Чт, Сб, Вс — берёт тему из базы знаний (папка content/) и красиво оформляет.
Пт — публикует лучшую новость недели из расширенного списка источников.
"""

import os
import json
import random
import logging
import hashlib
import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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

# Часовой пояс для определения дня недели
TIMEZONE = ZoneInfo("Europe/Moscow")

# Карта рубрик по дням недели (0=Пн, 1=Вт, ..., 6=Вс)
WEEKDAY_TOPICS = {
    0: "tools",       # Пн — Инструмент недели
    1: "lifehacks",   # Вт — Лайфхак
    2: "learning",    # Ср — Ресурс для обучения
    3: "production",  # Чт — Производство контента
    4: "news",        # Пт — Новость недели
    5: "prompts",     # Сб — Промпт дня
    6: "hidden",      # Вс — Малоизвестная нейронка
}

TOPIC_TITLES = {
    "tools": "🛠️ Инструмент недели",
    "lifehacks": "💡 Лайфхак дня",
    "learning": "📚 Учимся работать с ИИ",
    "production": "🎬 Производство AI-контента",
    "news": "🔥 Главная новость недели",
    "prompts": "✨ Промпт дня",
    "hidden": "🤖 Скрытая нейронка",
}

# RSS-источники для пятничных новостей
# Расширенный список с фокусом на инструменты создателей контента
RSS_SOURCES = [
    # Основные tech-источники про ИИ
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("Ars Technica AI", "https://arstechnica.com/ai/feed/"),
    # Hugging Face — много про новые модели
    ("Hugging Face Blog", "https://huggingface.co/blog/feed.xml"),
    # Reddit — фокус на генеративный контент
    ("Reddit StableDiffusion", "https://www.reddit.com/r/StableDiffusion/top/.rss?t=week"),
    ("Reddit SunoAI", "https://www.reddit.com/r/SunoAI/top/.rss?t=week"),
    ("Reddit aivideo", "https://www.reddit.com/r/aivideo/top/.rss?t=week"),
    ("Reddit ChatGPT", "https://www.reddit.com/r/ChatGPT/top/.rss?t=week"),
    # Русскоязычные
    ("Хабр ИИ", "https://habr.com/ru/rss/hub/artificial_intelligence/all/?fl=ru"),
    ("Хабр Машинное обучение", "https://habr.com/ru/rss/hub/machine_learning/all/?fl=ru"),
]

# Сколько новостей передавать Claude для выбора лучшей
NEWS_POOL_SIZE = 25

# Папка с базой знаний (контент для рубрик)
CONTENT_DIR = Path("content")
POSTED_FILE = Path("posted_news.json")
USED_TOPICS_FILE = Path("used_topics.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ============================================================
# ИСТОРИЯ ПУБЛИКАЦИЙ
# ============================================================

def load_posted_ids() -> set:
    if POSTED_FILE.exists():
        try:
            with open(POSTED_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f).get("ids", []))
        except Exception as e:
            logger.warning(f"Не удалось загрузить историю новостей: {e}")
    return set()


def save_posted_id(news_id: str) -> None:
    ids = load_posted_ids()
    ids.add(news_id)
    ids_list = list(ids)[-1000:]
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump({"ids": ids_list}, f, ensure_ascii=False, indent=2)


def load_used_topics() -> dict:
    """Хранит, какие темы из базы знаний уже публиковались."""
    if USED_TOPICS_FILE.exists():
        try:
            with open(USED_TOPICS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Не удалось загрузить used_topics: {e}")
    return {}


def save_used_topic(topic_type: str, filename: str) -> None:
    used = load_used_topics()
    if topic_type not in used:
        used[topic_type] = []
    used[topic_type].append(filename)
    # Храним последние 50 использованных тем по каждой рубрике
    used[topic_type] = used[topic_type][-50:]
    with open(USED_TOPICS_FILE, "w", encoding="utf-8") as f:
        json.dump(used, f, ensure_ascii=False, indent=2)


def make_news_id(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:16]


# ============================================================
# РАБОТА С БАЗОЙ ЗНАНИЙ
# ============================================================

def get_topic_from_knowledge_base(topic_type: str) -> dict | None:
    """Выбирает неиспользованную тему из соответствующей папки."""
    topic_dir = CONTENT_DIR / topic_type
    if not topic_dir.exists():
        logger.error(f"❌ Папка {topic_dir} не существует")
        return None

    # Все .md файлы в папке рубрики
    all_files = sorted([f for f in topic_dir.glob("*.md") if f.is_file()])
    if not all_files:
        logger.error(f"❌ В папке {topic_dir} нет файлов с темами")
        return None

    # Исключаем уже использованные
    used = load_used_topics().get(topic_type, [])
    available = [f for f in all_files if f.name not in used]

    # Если все темы прошли — начинаем по новой
    if not available:
        logger.info(f"♻️ Все темы {topic_type} уже использованы, начинаю круг заново")
        # Чистим историю по этой рубрике, чтоб можно было использовать снова
        used_data = load_used_topics()
        used_data[topic_type] = []
        with open(USED_TOPICS_FILE, "w", encoding="utf-8") as f:
            json.dump(used_data, f, ensure_ascii=False, indent=2)
        available = all_files

    # Берём случайную из доступных
    chosen_file = random.choice(available)
    logger.info(f"📂 Выбрана тема: {chosen_file.name}")

    try:
        with open(chosen_file, "r", encoding="utf-8") as f:
            content = f.read().strip()
        return {
            "type": topic_type,
            "filename": chosen_file.name,
            "content": content,
        }
    except Exception as e:
        logger.error(f"❌ Не удалось прочитать {chosen_file}: {e}")
        return None


# ============================================================
# ПРОМПТЫ ДЛЯ ОФОРМЛЕНИЯ ПОСТОВ ПО РУБРИКАМ
# ============================================================

WRITER_BASE = """Ты — автор Telegram-канала для создателей AI-контента. Аудитория — люди, которые сами делают видео и арт через нейронки (Suno, Runway, Kling, ChatGPT, ElevenLabs и т.д.). Они не новички, но всегда ищут новые приёмы и инструменты.

ТВОЯ ЗАДАЧА — оформить материал в короткий цепляющий пост.

ОБЩИЕ ПРАВИЛА:
- Пиши на русском
- Короткие предложения, без воды
- Максимум 600 символов в основном тексте
- 1-3 эмодзи, не больше
- Никакого канцелярита: "стоит отметить", "в современном мире", "важно понимать" — ЗАПРЕЩЕНО
- Начинай с крюка — фразы, которая заставит остановиться

HTML-ТЕГИ (только эти):
<b>жирный</b>
<i>курсив</i>
<code>код</code>
НИКАКИХ других тегов.

Не добавляй "Источник:", "Автор:", хэштеги — это всё добавится автоматически."""


PROMPTS_BY_TOPIC = {
    "tools": WRITER_BASE + """

РУБРИКА: ИНСТРУМЕНТ НЕДЕЛИ.
Тебе дан материал про конкретный AI-инструмент. Расскажи о нём так:

СТРУКТУРА:
1. Крюк — одно предложение, чем этот инструмент уникален
2. <b>Название инструмента</b>
3. Что он делает (1-2 предложения, по сути)
4. Главная фишка — за что его стоит попробовать
5. Кому это нужно (1 предложение)
6. Финальная мысль или вопрос

Тон: уверенный, как у опытного юзера, который тестит много инструментов.""",

    "lifehacks": WRITER_BASE + """

РУБРИКА: ЛАЙФХАК.
Тебе дан материал про конкретный приём или фишку. Оформи как готовый рецепт:

СТРУКТУРА:
1. Крюк — обещание результата ("Хочешь, чтобы X выдавал Y? Вот как.")
2. <b>Заголовок лайфхака</b> (короткий, конкретный)
3. Суть приёма — 2-3 предложения, что именно делать
4. Если есть код/параметр/промпт — выдели через <code>...</code>
5. Почему это работает (опционально, 1 предложение)
6. Призыв попробовать

Тон: дружеский, как будто делишься с коллегой.""",

    "learning": WRITER_BASE + """

РУБРИКА: РЕСУРС ДЛЯ ОБУЧЕНИЯ.
Тебе дан материал про полезный сайт, курс, канал или гайд для AI-творцов.

СТРУКТУРА:
1. Крюк — почему это стоит изучить
2. <b>Название ресурса</b>
3. Что там есть (1-2 предложения, конкретика)
4. Кому подойдёт и какой уровень
5. Один яркий пример, что можно вынести
6. Финальная фраза с рекомендацией

Тон: куратора, который сам прошёл этот ресурс.""",

    "production": WRITER_BASE + """

РУБРИКА: ПРОИЗВОДСТВО AI-КОНТЕНТА.
Тебе дан материал про конкретный приём, пайплайн или фишку производства AI-видео/арта.

СТРУКТУРА:
1. Крюк — какой результат даёт этот приём
2. <b>Название техники</b>
3. Как это устроено (2-3 предложения, по шагам если возможно)
4. Какие инструменты задействованы
5. На что обратить внимание / какая ошибка типичная
6. Призыв применить

Тон: практика, который сам прошёл через эти грабли.""",

    "prompts": WRITER_BASE + """

РУБРИКА: ПРОМПТ ДНЯ.
Тебе дан готовый промпт с примером результата.

СТРУКТУРА:
1. Крюк — какой эффект даёт промпт
2. <b>Название промпта или для чего он</b>
3. Сам промпт — обязательно в блоке <code>...</code> (можно многострочный)
4. Для какой нейронки (Suno / Midjourney / Kling и т.д.)
5. Что важно знать при использовании (1-2 предложения)
6. Призыв повторить и поделиться результатом

Можно увеличить длину поста до 800 символов, если промпт длинный.""",

    "hidden": WRITER_BASE + """

РУБРИКА: СКРЫТАЯ НЕЙРОНКА.
Тебе дан материал про малоизвестный или недооценённый AI-инструмент.

СТРУКТУРА:
1. Крюк — намёк на то, что эту нейронку мало кто знает, но она крутая
2. <b>Название инструмента</b>
3. Что он делает уникального
4. Чем лучше популярных аналогов
5. Где найти / как попробовать
6. Финальная мысль

Тон: открывателя сокровищ.""",

    "news": WRITER_BASE + """

РУБРИКА: ГЛАВНАЯ НОВОСТЬ НЕДЕЛИ.
Тебе даётся новость про ИИ. Перепиши её ярко и коротко.

СТРУКТУРА:
1. Крюк — что-то цепляющее, не "сегодня компания X объявила"
2. <b>Заголовок-суть</b>
3. Что произошло (2 предложения)
4. Почему это важно для создателей AI-контента (1-2 предложения)
5. Финальная провокация или вопрос

Тон: уверенный, без пафоса. Если новость переоценена — скажи это.""",
}


# ============================================================
# RSS-ПАРСИНГ (для пятничных новостей)
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
                })
        except Exception as e:
            logger.error(f"Ошибка при парсинге {source_name}: {e}")

    logger.info(f"📰 Всего свежих новостей: {len(all_news)}")
    return all_news


# ============================================================
# ВЫБОР ЛУЧШЕЙ НОВОСТИ (для пятницы)
# ============================================================

NEWS_SELECTOR_PROMPT = """Ты — главред крутого Telegram-канала для создателей AI-контента (видео, арт, музыка, тексты).
Твоя аудитория — практики, которые работают с Suno, Runway, Kling, ChatGPT, ElevenLabs, Stable Diffusion и подобными инструментами.

Из списка новостей выбери ОДНУ — самую интересную для этой аудитории за неделю.

ВЫСОКИЙ ПРИОРИТЕТ (нужно):
- Релизы новых генеративных моделей (видео, аудио, изображения)
- Большие обновления инструментов, которыми пользуются креаторы
- Новые возможности для AI-производства контента
- Прорывы в lipsync, motion, voice cloning
- Малоизвестные мощные инструменты, которые стоит знать

НИЗКИЙ ПРИОРИТЕТ (избегай):
- Корпоративные драмы (увольнения, инвестиции без продукта)
- Слухи и спекуляции
- 100500-я статья про то же самое
- Чисто академические работы без практической пользы
- Регуляторные новости и политика

ОТВЕТЬ ТОЛЬКО ОДНОЙ ЦИФРОЙ — номером лучшей новости. Без объяснений."""


def select_best_news(news_list: list[dict]) -> dict | None:
    if not news_list:
        return None
    if len(news_list) == 1:
        return news_list[0]

    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        candidates_text = "\n\n".join([
            f"[{i + 1}] ({n['source']}) {n['title']}\n{n['summary'][:400]}"
            for i, n in enumerate(news_list)
        ])

        logger.info(f"🤔 Claude выбирает лучшую из {len(news_list)} новостей...")
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=NEWS_SELECTOR_PROMPT,
            messages=[{"role": "user", "content": candidates_text}],
        )

        answer = response.content[0].text.strip()
        digits = "".join(c for c in answer if c.isdigit())
        if not digits:
            return news_list[0]

        index = int(digits) - 1
        if 0 <= index < len(news_list):
            chosen = news_list[index]
            logger.info(f"✅ Выбрана #{index + 1}: {chosen['title'][:60]}...")
            return chosen
        return news_list[0]

    except Exception as e:
        logger.error(f"❌ Ошибка при выборе новости: {e}")
        return news_list[0] if news_list else None


# ============================================================
# НАПИСАНИЕ ПОСТА
# ============================================================

def write_post(topic_type: str, material: str, source_info: str = "") -> str | None:
    """Универсальная функция написания поста под нужную рубрику."""
    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        system_prompt = PROMPTS_BY_TOPIC.get(topic_type, WRITER_BASE)

        user_message = f"Материал для поста:\n\n{material}"
        if source_info:
            user_message += f"\n\n(Источник: {source_info})"

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

        text = response.content[0].text.strip()
        logger.info(f"✏️ Пост написан, длина: {len(text)} символов")
        return text

    except Exception as e:
        logger.error(f"❌ Ошибка Claude API: {e}")
        return None


# ============================================================
# ПУБЛИКАЦИЯ
# ============================================================

async def publish_to_telegram(text: str, rubric_title: str, source_url: str | None = None, source_name: str | None = None) -> bool:
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)

        # Префикс с названием рубрики
        full_text = f"<b>{rubric_title}</b>\n\n{text}"

        # Хэштеги и опциональный источник
        hashtags = "#нейросети #AI"

        if source_url and source_name:
            full_text += f"\n\n🔗 <a href=\"{source_url}\">Источник: {source_name}</a>\n\n{hashtags}"
        else:
            full_text += f"\n\n{hashtags}"

        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=full_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        logger.info("✅ Пост опубликован")
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

    # Определяем рубрику по дню недели
    now = datetime.now(TIMEZONE)
    weekday = now.weekday()  # 0 = Пн
    topic_type = WEEKDAY_TOPICS[weekday]
    rubric_title = TOPIC_TITLES[topic_type]

    logger.info(f"📅 Сегодня {now.strftime('%A')}, рубрика: {topic_type}")
    logger.info(f"🎯 Готовлю пост: {rubric_title}")

    # ПЯТНИЦА — новости из RSS
    if topic_type == "news":
        news_list = fetch_news()
        if not news_list:
            logger.warning("⚠️ Нет свежих новостей")
            return

        candidates = news_list[:NEWS_POOL_SIZE]
        best = select_best_news(candidates)
        if not best:
            logger.warning("⚠️ Не удалось выбрать новость")
            return

        material = f"Заголовок: {best['title']}\n\nСодержание: {best['summary']}"
        post_text = write_post("news", material, source_info=best["source"])
        if not post_text:
            return

        success = await publish_to_telegram(
            text=post_text,
            rubric_title=rubric_title,
            source_url=best["url"],
            source_name=best["source"],
        )
        if success:
            save_posted_id(best["id"])
        return

    # ОСТАЛЬНЫЕ ДНИ — берём тему из базы знаний
    topic = get_topic_from_knowledge_base(topic_type)
    if not topic:
        logger.error(f"❌ Нет доступных тем в рубрике {topic_type}")
        return

    post_text = write_post(topic_type, topic["content"])
    if not post_text:
        return

    success = await publish_to_telegram(
        text=post_text,
        rubric_title=rubric_title,
    )
    if success:
        save_used_topic(topic_type, topic["filename"])


if __name__ == "__main__":
    asyncio.run(main())
