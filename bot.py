"""
Telegram-бот для AI-канала (версия 6).
Чётные дни — свежая новость из быстрых источников.
Нечётные дни — пост по теме из topics.txt: бот САМ ищет свежую
информацию в интернете (web search через Claude API) и пишет пост.
Больше никакой статичной базы, которая кончается и идёт по кругу.
"""

import os
import json
import random
import logging
import hashlib
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import urllib.request
from anthropic import Anthropic
from telegram import Bot
from telegram.constants import ParseMode

# ============================================================
# НАСТРОЙКИ
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

TIMEZONE = ZoneInfo("Europe/Moscow")

# Источники новостей (быстрые)
RSS_SOURCES = [
    ("r/aivideo", "https://www.reddit.com/r/aivideo/top/.rss?t=week"),
    ("r/StableDiffusion", "https://www.reddit.com/r/StableDiffusion/top/.rss?t=week"),
    ("r/SunoAI", "https://www.reddit.com/r/SunoAI/top/.rss?t=week"),
    ("r/singularity", "https://www.reddit.com/r/singularity/top/.rss?t=week"),
    ("r/LocalLLaMA", "https://www.reddit.com/r/LocalLLaMA/top/.rss?t=week"),
    ("r/artificial", "https://www.reddit.com/r/artificial/top/.rss?t=week"),
    ("r/midjourney", "https://www.reddit.com/r/midjourney/top/.rss?t=week"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("Ars Technica AI", "https://arstechnica.com/ai/feed/"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
]

FRESHNESS_DAYS = 4        # окно свежести новостей
NEWS_POOL_SIZE = 30       # сколько кандидатов даём селектору
RECENT_TITLES_MEMORY = 15 # сколько последних тем помним для анти-повторов
WEB_SEARCH_MAX_USES = 4   # лимит поисков на один пост (контроль расходов)

TOPICS_FILE = Path("topics.txt")
POSTED_FILE = Path("posted_news.json")
USED_TOPICS_FILE = Path("used_topics.json")

MODEL = "claude-haiku-4-5-20251001"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# ============================================================
# ХРАНИЛИЩЕ
# ============================================================

def load_json(path: Path, default):
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Не прочитать {path}: {e}")
    return default


def save_json(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_posted() -> dict:
    return load_json(POSTED_FILE, {"ids": [], "titles": []})


def save_posted(news_id: str, title: str):
    data = load_posted()
    ids = set(data.get("ids", []))
    ids.add(news_id)
    titles = data.get("titles", [])
    titles.append(title[:100])
    save_json(POSTED_FILE, {
        "ids": list(ids)[-1000:],
        "titles": titles[-RECENT_TITLES_MEMORY:],
    })


def make_news_id(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:16]


# ============================================================
# ПАРСИНГ НОВОСТЕЙ
# ============================================================

def fetch_feed_with_ua(url: str):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TelegramAINewsBot/1.0"
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            return feedparser.parse(resp.read())
    except Exception as e:
        logger.warning(f"UA-парсинг упал для {url}: {e}")
        try:
            return feedparser.parse(url)
        except Exception:
            return None


def entry_age_days(entry) -> float | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                dt = datetime(*t[:6], tzinfo=timezone.utc)
                return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
            except Exception:
                continue
    return None


def fetch_news() -> list[dict]:
    posted_ids = set(load_posted().get("ids", []))
    all_news = []
    for source_name, rss_url in RSS_SOURCES:
        feed = fetch_feed_with_ua(rss_url)
        if not feed or not feed.entries:
            continue
        for entry in feed.entries[:15]:
            url = entry.get("link", "")
            if not url:
                continue
            news_id = make_news_id(url)
            if news_id in posted_ids:
                continue
            age = entry_age_days(entry)
            if age is not None and age > FRESHNESS_DAYS:
                continue
            summary = entry.get("summary", "") or entry.get("description", "")
            for tag in ["<p>", "</p>", "<br>", "<br/>", "<div>", "</div>", "<span>", "</span>"]:
                summary = summary.replace(tag, " ")
            all_news.append({
                "id": news_id,
                "title": entry.get("title", "").strip(),
                "summary": summary.strip()[:1200],
                "url": url,
                "source": source_name,
                "age_days": round(age, 1) if age is not None else None,
            })
    logger.info(f"📰 Свежих новостей: {len(all_news)}")
    return all_news


# ============================================================
# ВЫБОР НОВОСТИ (с анти-повтором тем)
# ============================================================

NEWS_SELECTOR_PROMPT = """Ты — главред русскоязычного Telegram-канала для AI-креаторов (видео, музыка, арт через нейросети).

Из списка выбери ОДНУ новость — самую горячую и полезную для практиков.

ПРИОРИТЕТ: релизы и обновления генеративных моделей, новые возможности инструментов, прорывы в lipsync/длине/качестве/цене, полезные open-source находки.
ИЗБЕГАЙ: корпоративные драмы, политика, чистая наука без применения, мутные слухи.

КРИТИЧЕСКИ ВАЖНО — РАЗНООБРАЗИЕ: тебе дадут список недавно опубликованных тем. НЕ выбирай новость, тематически похожую на них. Если про инструмент X недавно был пост — выбери новость про другой инструмент или другую область (звук вместо видео, open-source вместо релизов и т.д.). Разнообразие важнее «горячести».

Ответь ТОЛЬКО номером. Одна цифра, без пояснений."""


def select_best_news(news_list: list[dict]) -> dict | None:
    if not news_list:
        return None
    if len(news_list) == 1:
        return news_list[0]
    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        recent = load_posted().get("titles", [])
        recent_block = "\n".join(f"- {t}" for t in recent) if recent else "(пока пусто)"
        candidates = "\n\n".join([
            f"[{i+1}] ({n['source']}, {n['age_days']}д) {n['title']}\n{n['summary'][:250]}"
            for i, n in enumerate(news_list)
        ])
        msg = f"НЕДАВНО ОПУБЛИКОВАННЫЕ ТЕМЫ (избегай похожих):\n{recent_block}\n\nКАНДИДАТЫ:\n\n{candidates}"
        resp = client.messages.create(
            model=MODEL, max_tokens=10,
            system=NEWS_SELECTOR_PROMPT,
            messages=[{"role": "user", "content": msg}],
        )
        digits = "".join(c for c in resp.content[0].text if c.isdigit())
        if digits:
            idx = int(digits) - 1
            if 0 <= idx < len(news_list):
                logger.info(f"✅ Выбрано #{idx+1}: {news_list[idx]['title'][:60]}")
                return news_list[idx]
        return news_list[0]
    except Exception as e:
        logger.error(f"Ошибка выбора: {e}")
        return news_list[0]


NEWS_WRITER_PROMPT = """Ты — автор Telegram-канала про AI для создателей контента. Стиль — живой, экспертный, с мнением.

СТРУКТУРА:
1. Заголовок жирным с эмодзи в начале — ёмкий, честный
2. Одна вводная строка — почему это важно
3. Блок «что нового» — 3-5 пунктов через тире с <b>ключевым словом</b>
4. Твоя оценка — 1-2 предложения экспертного взгляда, честно (если переоценено — скажи)
5. Финал — вывод, прогноз или вопрос

СТИЛЬ: от первого лица, живой язык, лёгкая дерзость без кринжа. Конкретика вместо воды.
ЗАПРЕЩЕНО: «компания объявила», канцелярит, выдумывание фактов которых нет в новости.
ДЛИНА: до 900 символов.
HTML: только <b>, <i>, <code>.
Не добавляй источник и хэштеги — добавятся автоматически."""


def write_news_post(news: dict) -> str | None:
    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = f"Заголовок: {news['title']}\nИсточник: {news['source']}\nСодержание: {news['summary']}\n\nТолько факты из новости."
        resp = client.messages.create(
            model=MODEL, max_tokens=1200,
            system=NEWS_WRITER_PROMPT,
            messages=[{"role": "user", "content": msg}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Ошибка написания новости: {e}")
        return None


# ============================================================
# ДИНАМИЧЕСКИЕ ТЕМЫ С ВЕБ-ПОИСКОМ
# ============================================================

def load_topics() -> list[str]:
    """Читает topics.txt, отбрасывает комментарии и пустые строки."""
    if not TOPICS_FILE.exists():
        logger.error("Нет файла topics.txt")
        return []
    lines = TOPICS_FILE.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def pick_topic() -> str | None:
    """Выбирает случайную неиспользованную тему. Круг пройден — сброс."""
    topics = load_topics()
    if not topics:
        return None
    used_data = load_json(USED_TOPICS_FILE, {})
    used = set(used_data.get("dynamic_used", []))
    available = [t for t in topics if t not in used]
    if not available:
        logger.info("♻️ Все темы пройдены — начинаю новый круг (инфа обновится веб-поиском)")
        used_data["dynamic_used"] = []
        save_json(USED_TOPICS_FILE, used_data)
        available = topics
    return random.choice(available)


def mark_topic_used(topic: str):
    used_data = load_json(USED_TOPICS_FILE, {})
    used_data.setdefault("dynamic_used", []).append(topic)
    save_json(USED_TOPICS_FILE, used_data)


DYNAMIC_WRITER_PROMPT = """Ты — автор русскоязычного Telegram-канала для AI-креаторов (делают видео, музыку, арт через нейросети). Аудитория — практики, не новички.

ЗАДАЧА: тебе дают тему. Найди через веб-поиск СВЕЖУЮ информацию по ней (актуальное состояние, последние версии, цены, фишки) и напиши цепляющий пост.

ПРАВИЛА ПОИСКА:
- Ищи актуальную информацию: текущие возможности, свежие обновления, реальные цены
- Опирайся ТОЛЬКО на найденное. Не выдумывай факты, версии, цифры
- Если данные противоречат друг другу — бери более свежий источник

СТРУКТУРА ПОСТА:
1. Крюк — первая строка, цепляет и обещает пользу
2. <b>Заголовок темы</b>
3. Суть — конкретные факты, цифры, названия из поиска (3-6 коротких абзацев или пунктов)
4. Практический вывод — что с этим делать читателю
5. Финальная фраза или вопрос

СТИЛЬ: живой, от практика, лёгкая дерзость, без канцелярита («стоит отметить» — запрещено). 1-3 эмодзи.
ДЛИНА: 600-900 символов. Если тема — промпт или инструкция, можно до 1100.
HTML: только <b>, <i>, <code>. Промпты и параметры оформляй в <code>.
Не добавляй ссылки, источники и хэштеги — добавятся автоматически.

ВАЖНО: в финальном ответе выдай ТОЛЬКО текст поста. Без преамбул «вот пост», без пояснений."""


def write_dynamic_post(topic: str) -> str | None:
    """Пишет пост по теме, используя веб-поиск для свежих фактов."""
    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        messages = [{"role": "user", "content":
                     f"Тема поста: {topic}\n\nНайди свежую информацию и напиши пост."}]
        tools = [{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": WEB_SEARCH_MAX_USES,
        }]

        # Цикл на случай pause_turn (поиск может идти в несколько заходов)
        for _ in range(4):
            resp = client.messages.create(
                model=MODEL,
                max_tokens=2000,
                system=DYNAMIC_WRITER_PROMPT,
                messages=messages,
                tools=tools,
            )
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            break

        text = "".join(
            block.text for block in resp.content
            if getattr(block, "type", "") == "text"
        ).strip()

        if len(text) < 100:
            logger.warning(f"Слишком короткий результат ({len(text)} симв.)")
            return None
        logger.info(f"✏️ Динамический пост: {len(text)} символов")
        return text
    except Exception as e:
        logger.error(f"Ошибка динамического поста: {e}")
        return None


# ============================================================
# ПУБЛИКАЦИЯ
# ============================================================

async def publish(text: str, source_url: str | None = None,
                  source_name: str | None = None) -> bool:
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        parts = [text]
        if source_url and source_name:
            parts.append(f"\n🔗 <a href=\"{source_url}\">{source_name}</a>")
        parts.append("\n#нейросети #AI #ИИ")
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text="\n".join(parts),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        logger.info("✅ Опубликовано")
        return True
    except Exception as e:
        logger.error(f"Ошибка публикации: {e}")
        # Запасной вариант: если HTML сломан — публикуем без разметки
        try:
            import re
            plain = re.sub(r"<[^>]+>", "", text)
            await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=plain)
            logger.info("✅ Опубликовано без разметки (HTML был сломан)")
            return True
        except Exception as e2:
            logger.error(f"И без разметки не вышло: {e2}")
            return False


# ============================================================
# ОСНОВНАЯ ЛОГИКА
# ============================================================

async def do_news() -> bool:
    news_list = fetch_news()
    if not news_list:
        return False
    best = select_best_news(news_list[:NEWS_POOL_SIZE])
    if not best:
        return False
    text = write_news_post(best)
    if not text:
        return False
    ok = await publish(text, best["url"], best["source"])
    if ok:
        save_posted(best["id"], best["title"])
    return ok


async def do_dynamic_topic() -> bool:
    topic = pick_topic()
    if not topic:
        return False
    logger.info(f"🎯 Тема дня: {topic}")
    text = write_dynamic_post(topic)
    if not text:
        return False
    ok = await publish(text)
    if ok:
        mark_topic_used(topic)
        # Тоже запоминаем в анти-повторы новостного селектора
        data = load_posted()
        titles = data.get("titles", [])
        titles.append(topic[:100])
        data["titles"] = titles[-RECENT_TITLES_MEMORY:]
        save_json(POSTED_FILE, data)
    return ok


async def main():
    missing = [k for k, v in {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHANNEL_ID": TELEGRAM_CHANNEL_ID,
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
    }.items() if not v]
    if missing:
        logger.error(f"Не заданы секреты: {', '.join(missing)}")
        raise SystemExit(1)

    day = datetime.now(TIMEZONE).day
    is_news_day = (day % 2 == 0)
    logger.info(f"📅 День {day}: {'НОВОСТЬ' if is_news_day else 'ТЕМА С ВЕБ-ПОИСКОМ'}")

    if is_news_day:
        if not await do_news():
            logger.info("Новостей нет — пишу тему с веб-поиском")
            await do_dynamic_topic()
    else:
        if not await do_dynamic_topic():
            logger.info("Тема не вышла — публикую новость")
            await do_news()


if __name__ == "__main__":
    asyncio.run(main())
