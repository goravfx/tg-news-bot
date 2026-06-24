"""
Telegram-бот для AI-канала (версия 5).
Чередует свежие новости и рубрики из базы знаний.
Новости берутся из быстрых источников (Reddit + спец-сайты),
фильтруются по свежести и переписываются в стиле эксперта с мнением.
"""
 
import os
import json
import time
import random
import logging
import hashlib
import asyncio
from datetime import datetime, timezone, timedelta
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
 
# Чередование: чётные дни месяца — НОВОСТИ, нечётные — РУБРИКА из базы.
# (можно поменять логику ниже в main)
 
# ----- ИСТОЧНИКИ НОВОСТЕЙ (быстрые, специализированные) -----
# Reddit — новости появляются в день релиза.
# Топ за неделю = только то, что реально завирусилось.
RSS_SOURCES = [
    # Reddit — генеративный контент (САМЫЕ БЫСТРЫЕ)
    ("r/aivideo", "https://www.reddit.com/r/aivideo/top/.rss?t=week"),
    ("r/StableDiffusion", "https://www.reddit.com/r/StableDiffusion/top/.rss?t=week"),
    ("r/SunoAI", "https://www.reddit.com/r/SunoAI/top/.rss?t=week"),
    ("r/singularity", "https://www.reddit.com/r/singularity/top/.rss?t=week"),
    ("r/LocalLLaMA", "https://www.reddit.com/r/LocalLLaMA/top/.rss?t=week"),
    ("r/artificial", "https://www.reddit.com/r/artificial/top/.rss?t=week"),
    ("r/midjourney", "https://www.reddit.com/r/midjourney/top/.rss?t=week"),
    # Спец-сайты про AI-инструменты
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("Ars Technica AI", "https://arstechnica.com/ai/feed/"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
]
 
# Окно свежести: берём только новости за последние N дней
FRESHNESS_DAYS = 4
 
# Сколько новостей даём Claude для выбора лучшей
NEWS_POOL_SIZE = 30
 
# Папки
CONTENT_DIR = Path("content")
POSTED_FILE = Path("posted_news.json")
USED_TOPICS_FILE = Path("used_topics.json")
 
# Рубрики из базы знаний (для дней-рубрик)
TOPIC_ROTATION = ["tools", "lifehacks", "learning", "production", "prompts", "hidden"]
TOPIC_TITLES = {
    "tools": "🛠️ Инструмент",
    "lifehacks": "💡 Лайфхак",
    "learning": "📚 Учимся работать с ИИ",
    "production": "🎬 Производство AI-контента",
    "prompts": "✨ Промпт дня",
    "hidden": "🤖 Скрытая нейронка",
}
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
 
 
# ============================================================
# ИСТОРИЯ
# ============================================================
 
def load_json(path: Path, default):
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Не удалось прочитать {path}: {e}")
    return default
 
 
def save_json(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
 
 
def load_posted_ids() -> set:
    return set(load_json(POSTED_FILE, {"ids": []}).get("ids", []))
 
 
def save_posted_id(news_id: str):
    ids = load_posted_ids()
    ids.add(news_id)
    save_json(POSTED_FILE, {"ids": list(ids)[-1000:]})
 
 
def make_news_id(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
 
 
# ============================================================
# ПАРСИНГ С ФИЛЬТРОМ СВЕЖЕСТИ
# ============================================================
 
def fetch_feed_with_ua(url: str):
    """Парсит RSS с User-Agent (нужно для Reddit, иначе блок)."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TelegramAINewsBot/1.0"
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
        return feedparser.parse(data)
    except Exception as e:
        logger.warning(f"UA-парсинг не сработал для {url}: {e}, пробую обычный")
        try:
            return feedparser.parse(url)
        except Exception as e2:
            logger.error(f"И обычный парсинг упал: {e2}")
            return None
 
 
def entry_age_days(entry) -> float | None:
    """Возраст записи в днях. None если дату не определить."""
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
    posted_ids = load_posted_ids()
    all_news = []
 
    for source_name, rss_url in RSS_SOURCES:
        feed = fetch_feed_with_ua(rss_url)
        if not feed or not feed.entries:
            logger.info(f"Пусто: {source_name}")
            continue
 
        logger.info(f"Парсим {source_name}: {len(feed.entries)} записей")
        for entry in feed.entries[:15]:
            url = entry.get("link", "")
            if not url:
                continue
 
            news_id = make_news_id(url)
            if news_id in posted_ids:
                continue
 
            # ФИЛЬТР СВЕЖЕСТИ
            age = entry_age_days(entry)
            if age is not None and age > FRESHNESS_DAYS:
                continue  # слишком старое
 
            summary = entry.get("summary", "") or entry.get("description", "")
            for tag in ["<p>", "</p>", "<br>", "<br/>", "<div>", "</div>", "<span>", "</span>"]:
                summary = summary.replace(tag, " ")
 
            title = entry.get("title", "").strip()
            # Reddit-заголовки часто содержат флейр в скобках — оставляем как есть
 
            all_news.append({
                "id": news_id,
                "title": title,
                "summary": summary.strip()[:1200],
                "url": url,
                "source": source_name,
                "age_days": round(age, 1) if age is not None else None,
            })
 
    logger.info(f"📰 Свежих новостей (до {FRESHNESS_DAYS} дн.): {len(all_news)}")
    return all_news
 
 
# ============================================================
# ВЫБОР ЛУЧШЕЙ НОВОСТИ
# ============================================================
 
NEWS_SELECTOR_PROMPT = """Ты — главред топового русскоязычного Telegram-канала для AI-креаторов (делают видео, музыку, арт через нейросети).
Аудитория — практики: работают с Seedance, Kling, Veo, Suno, ElevenLabs, Nano Banana, Seedream и т.д.
 
Из списка выбери ОДНУ новость — самую горячую и полезную для этой аудитории ПРЯМО СЕЙЧАС.
 
МАКСИМАЛЬНЫЙ ПРИОРИТЕТ:
- Релизы и крупные обновления генеративных моделей (видео/аудио/изображения)
- Новые возможности инструментов создателей контента
- Прорывы: lipsync, длина видео, разрешение, консистентность, скорость, цена
- Бенчмарки, где модель обошла конкурентов
 
НИЗКИЙ ПРИОРИТЕТ (избегай):
- Корпоративные драмы, инвестиции, увольнения
- Политика, регуляции, этические споры без продукта
- Чистая наука без практического применения
- Мутные слухи без конкретики
 
ВАЖНО: если в списке есть свежий релиз модели (Seedance, Kling, Veo, Suno, Nano Banana и подобные) — почти всегда это лучший выбор.
 
Ответь ТОЛЬКО номером лучшей новости. Одна цифра, без пояснений."""
 
 
def select_best_news(news_list: list[dict]) -> dict | None:
    if not news_list:
        return None
    if len(news_list) == 1:
        return news_list[0]
    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        candidates = "\n\n".join([
            f"[{i+1}] ({n['source']}, {n['age_days']}д назад) {n['title']}\n{n['summary'][:300]}"
            for i, n in enumerate(news_list)
        ])
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=NEWS_SELECTOR_PROMPT,
            messages=[{"role": "user", "content": candidates}],
        )
        answer = resp.content[0].text.strip()
        digits = "".join(c for c in answer if c.isdigit())
        if digits:
            idx = int(digits) - 1
            if 0 <= idx < len(news_list):
                chosen = news_list[idx]
                logger.info(f"✅ Выбрано #{idx+1}: {chosen['title'][:60]}")
                return chosen
        return news_list[0]
    except Exception as e:
        logger.error(f"Ошибка выбора: {e}")
        return news_list[0]
 
 
# ============================================================
# НАПИСАНИЕ ПОСТА — НОВОСТЬ (стиль эксперта)
# ============================================================
 
NEWS_WRITER_PROMPT = """Ты — автор крутого Telegram-канала про AI для создателей контента. Твой стиль — живой, экспертный, с мнением. Не пересказчик новостей, а practitioner, который сам в теме и делится находками.
 
ЗАДАЧА: превратить новость в цепляющий пост в стиле топовых AI-каналов.
 
СТРУКТУРА:
1. ЗАГОЛОВОК жирным с эмодзи в начале — ёмкий, кликбейтный но честный. Примеры: "🚀 Seedance 2.5 — это уже перебор (в хорошем смысле)", "🔥 Kling выкатил Turbo и это меняет правила"
2. ОДНА вводная строка — почему это важно/круто. Цепляет и держит.
3. БЛОК "Что нового" — 3-5 пунктов через тире, каждый с <b>ключевым словом</b> жирным. Конкретика: цифры, фичи, отличия от прошлой версии.
4. ТВОЯ ОЦЕНКА — 1-2 предложения личного экспертного взгляда. Что это значит для практика. Где подвох или где реально прорыв. Будь честным — если фича переоценена, скажи.
5. ФИНАЛ — короткий вывод, прогноз или вопрос к аудитории.
 
СТИЛЬ:
- От первого лица, как эксперт: "Я бы сказал...", "На мой взгляд...", "Это тот случай, когда..."
- Живой язык, лёгкая дерзость, можно сленг ("выкатили", "имба", "перебор", "разрыв")
- НО без кринжа и без переигрывания
- Конкретика вместо воды. Цифры, факты, отличия.
 
ЗАПРЕЩЕНО:
- "Компания X объявила о выпуске" — скучно
- "В современном мире", "стоит отметить", "важно понимать"
- Канцелярит и пресс-релизный тон
- Выдумывать факты которых нет в новости. Если чего-то не знаешь — не пиши.
 
ВАЖНО ПРО ТОЧНОСТЬ:
Опирайся ТОЛЬКО на факты из новости. Не добавляй характеристики, цифры, даты, которых нет в исходнике. Лучше меньше деталей, но правда.
 
ДЛИНА: до 900 символов.
 
HTML: только <b>жирный</b>, <i>курсив</i>, <code>код</code>. Других тегов нет.
 
Не добавляй "Источник:" и хэштеги — добавится автоматически."""
 
 
def write_news_post(news: dict) -> str | None:
    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = f"""Напиши пост по этой новости.
 
Заголовок: {news['title']}
Источник: {news['source']}
Содержание: {news['summary']}
 
Помни: только факты из новости, не выдумывай детали."""
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            system=NEWS_WRITER_PROMPT,
            messages=[{"role": "user", "content": msg}],
        )
        text = resp.content[0].text.strip()
        logger.info(f"✏️ Новостной пост: {len(text)} символов")
        return text
    except Exception as e:
        logger.error(f"Ошибка написания новости: {e}")
        return None
 
 
# ============================================================
# РУБРИКИ ИЗ БАЗЫ ЗНАНИЙ
# ============================================================
 
def get_topic_from_base(topic_type: str) -> dict | None:
    topic_dir = CONTENT_DIR / topic_type
    if not topic_dir.exists():
        logger.error(f"Нет папки {topic_dir}")
        return None
    all_files = sorted([f for f in topic_dir.glob("*.md") if f.is_file()])
    if not all_files:
        return None
    used = load_json(USED_TOPICS_FILE, {}).get(topic_type, [])
    available = [f for f in all_files if f.name not in used]
    if not available:
        # круг пройден — сбрасываем
        used_data = load_json(USED_TOPICS_FILE, {})
        used_data[topic_type] = []
        save_json(USED_TOPICS_FILE, used_data)
        available = all_files
    chosen = random.choice(available)
    try:
        content = chosen.read_text(encoding="utf-8").strip()
        return {"type": topic_type, "filename": chosen.name, "content": content}
    except Exception as e:
        logger.error(f"Не прочитать {chosen}: {e}")
        return None
 
 
def save_used_topic(topic_type: str, filename: str):
    used = load_json(USED_TOPICS_FILE, {})
    used.setdefault(topic_type, []).append(filename)
    used[topic_type] = used[topic_type][-50:]
    save_json(USED_TOPICS_FILE, used)
 
 
TOPIC_WRITER_BASE = """Ты — автор Telegram-канала для AI-креаторов. Аудитория делает видео/арт/музыку через нейросети. Не новички, ищут приёмы и инструменты.
 
Оформи материал в короткий цепляющий пост.
 
ОБЩИЕ ПРАВИЛА:
- Русский язык, живой тон, лёгкая дерзость
- Короткие предложения, без воды
- Максимум 700 символов
- 1-3 эмодзи
- Никакого канцелярита: "стоит отметить", "в современном мире" — ЗАПРЕЩЕНО
- Начинай с крюка
- НЕ выдумывай факты которых нет в материале
 
HTML: <b>жирный</b>, <i>курсив</i>, <code>код</code>. Других тегов нет.
Не добавляй "Источник:" и хэштеги — добавится автоматически."""
 
TOPIC_PROMPTS = {
    "tools": TOPIC_WRITER_BASE + "\n\nРУБРИКА: ИНСТРУМЕНТ. Расскажи про инструмент: крюк → <b>название</b> → что делает → главная фишка → кому нужно → вывод.",
    "lifehacks": TOPIC_WRITER_BASE + "\n\nРУБРИКА: ЛАЙФХАК. Дай приём как рецепт: крюк-обещание → <b>суть</b> → шаги → если есть код/промпт выдели <code>...</code> → призыв попробовать.",
    "learning": TOPIC_WRITER_BASE + "\n\nРУБРИКА: ОБУЧЕНИЕ. Расскажи про ресурс: крюк → <b>название</b> → что внутри → кому подойдёт → что вынесешь.",
    "production": TOPIC_WRITER_BASE + "\n\nРУБРИКА: ПРОИЗВОДСТВО. Опиши технику/пайплайн: крюк → <b>название</b> → как устроено по шагам → инструменты → типичная ошибка → призыв.",
    "prompts": TOPIC_WRITER_BASE + "\n\nРУБРИКА: ПРОМПТ. Дай готовый промпт: крюк → для чего → сам промпт в <code>...</code> → для какой нейронки → что важно знать. Можно до 900 символов.",
    "hidden": TOPIC_WRITER_BASE + "\n\nРУБРИКА: СКРЫТАЯ НЕЙРОНКА. Открой малоизвестный инструмент: крюк-интрига → <b>название</b> → что умеет уникального → чем лучше популярных → где найти.",
}
 
 
def write_topic_post(topic_type: str, material: str) -> str | None:
    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        system = TOPIC_PROMPTS.get(topic_type, TOPIC_WRITER_BASE)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1100,
            system=system,
            messages=[{"role": "user", "content": f"Материал:\n\n{material}"}],
        )
        text = resp.content[0].text.strip()
        logger.info(f"✏️ Рубричный пост: {len(text)} символов")
        return text
    except Exception as e:
        logger.error(f"Ошибка написания рубрики: {e}")
        return None
 
 
# ============================================================
# ПУБЛИКАЦИЯ
# ============================================================
 
async def publish(text: str, rubric_title: str | None,
                  source_url: str | None, source_name: str | None) -> bool:
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        parts = []
        if rubric_title:
            parts.append(f"<b>{rubric_title}</b>\n")
        parts.append(text)
        if source_url and source_name:
            parts.append(f"\n🔗 <a href=\"{source_url}\">{source_name}</a>")
        parts.append("\n#нейросети #AI #ИИ")
        full = "\n".join(parts)
 
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=full,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        logger.info("✅ Опубликовано")
        return True
    except Exception as e:
        logger.error(f"Ошибка публикации: {e}")
        return False
 
 
# ============================================================
# ОСНОВНАЯ ЛОГИКА
# ============================================================
 
async def do_news() -> bool:
    """Опубликовать свежую новость. Возвращает True если успешно."""
    news_list = fetch_news()
    if not news_list:
        logger.warning("Нет свежих новостей")
        return False
    candidates = news_list[:NEWS_POOL_SIZE]
    best = select_best_news(candidates)
    if not best:
        return False
    text = write_news_post(best)
    if not text:
        return False
    ok = await publish(text, None, best["url"], best["source"])
    if ok:
        save_posted_id(best["id"])
    return ok
 
 
async def do_topic() -> bool:
    """Опубликовать рубрику из базы. Возвращает True если успешно."""
    # Честная ротация: берём следующую рубрику по счётчику, а не по дню.
    # Счётчик хранится в used_topics.json под ключом "_rotation_index".
    used = load_json(USED_TOPICS_FILE, {})
    idx = used.get("_rotation_index", 0)
    topic_type = TOPIC_ROTATION[idx % len(TOPIC_ROTATION)]
    used["_rotation_index"] = (idx + 1) % len(TOPIC_ROTATION)
    save_json(USED_TOPICS_FILE, used)
    logger.info(f"Рубрика по ротации: {topic_type}")
 
    topic = get_topic_from_base(topic_type)
    if not topic:
        logger.warning(f"Нет тем в рубрике {topic_type}")
        return False
    text = write_topic_post(topic_type, topic["content"])
    if not text:
        return False
    ok = await publish(text, TOPIC_TITLES[topic_type], None, None)
    if ok:
        save_used_topic(topic_type, topic["filename"])
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
 
    # ЧЕРЕДОВАНИЕ: чётные дни месяца — новости, нечётные — рубрика
    day = datetime.now(TIMEZONE).day
    is_news_day = (day % 2 == 0)
 
    logger.info(f"📅 День {day}, режим: {'НОВОСТИ' if is_news_day else 'РУБРИКА'}")
 
    if is_news_day:
        # Новость, с откатом на рубрику если новостей нет
        if not await do_news():
            logger.info("Новостей нет — публикую рубрику вместо них")
            await do_topic()
    else:
        # Рубрика, с откатом на новость если база пуста
        if not await do_topic():
            logger.info("База пуста — публикую новость вместо рубрики")
            await do_news()
 
 
if __name__ == "__main__":
    asyncio.run(main())
