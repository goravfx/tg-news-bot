"""
Telegram-бот для AI-канала (версия 7).

Три режима по дням:
  - НОВОСТЬ: сбор из сгруппированных источников, отсев по возрасту,
    детект "уже у всех", оценка тем, выбор лучшей.
  - ТЕМА: пост по теме из topics.txt с веб-поиском свежих фактов.
  - ТИХИЕ ИЗМЕНЕНИЯ: отдельная охота за сменой цен/лимитов/доступа.
"""

import os
import re
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
MODEL = "claude-haiku-4-5-20251001"

# ---------- ИСТОЧНИКИ, РАЗБИТЫЕ НА ГРУППЫ ----------
# Группы важнее количества: каждая даёт свой тип новостей.
# take — сколько записей берём из каждого фида группы (квота против засилья).
SOURCE_GROUPS = {
    "техбаза": {
        "take": 6,
        "feeds": [
            ("Hacker News", "https://hnrss.org/frontpage?points=150"),
            ("Show HN", "https://hnrss.org/show"),
            ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
        ],
    },
    "лаборатории": {
        "take": 6,
        "feeds": [
            ("Anthropic", "https://www.anthropic.com/news/rss.xml"),
            ("OpenAI", "https://openai.com/blog/rss.xml"),
            ("Google AI", "https://blog.google/technology/ai/rss/"),
            ("Qwen", "https://qwenlm.github.io/blog/index.xml"),
        ],
    },
    "сообщества": {
        "take": 5,
        "feeds": [
            ("r/aivideo", "https://www.reddit.com/r/aivideo/top/.rss?t=week"),
            ("r/StableDiffusion", "https://www.reddit.com/r/StableDiffusion/top/.rss?t=week"),
            ("r/SunoAI", "https://www.reddit.com/r/SunoAI/top/.rss?t=week"),
            ("r/LocalLLaMA", "https://www.reddit.com/r/LocalLLaMA/top/.rss?t=week"),
            ("r/singularity", "https://www.reddit.com/r/singularity/top/.rss?t=week"),
            ("r/midjourney", "https://www.reddit.com/r/midjourney/top/.rss?t=week"),
        ],
    },
    "новости": {
        "take": 5,
        "feeds": [
            ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
            ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
            ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
            ("Ars Technica AI", "https://arstechnica.com/ai/feed/"),
        ],
    },
    "русскоязычное": {
        "take": 5,
        "feeds": [
            ("Хабр ИИ", "https://habr.com/ru/rss/hub/artificial_intelligence/all/?fl=ru"),
            ("Хабр ML", "https://habr.com/ru/rss/hub/machine_learning/all/?fl=ru"),
        ],
    },
}

FRESHNESS_HOURS = 48        # жёсткий отсев: новости старше 48 часов не проходят
DROP_UNDATED = True         # без подтверждённой даты — в мусор
NEWS_POOL_SIZE = 28         # сколько кандидатов уходит на оценку
EVERYWHERE_THRESHOLD = 4    # если история в N+ источниках — считаем "уже у всех"
RECENT_TITLES_MEMORY = 20   # память для анти-повторов
WEB_SEARCH_MAX_USES = 5

TOPICS_FILE = Path("topics.txt")
POSTED_FILE = Path("posted_news.json")
USED_TOPICS_FILE = Path("used_topics.json")

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


def remember_title(title: str):
    data = load_posted()
    titles = data.get("titles", [])
    titles.append(title[:120])
    data["titles"] = titles[-RECENT_TITLES_MEMORY:]
    save_json(POSTED_FILE, data)


def save_posted(news_id: str, title: str):
    data = load_posted()
    ids = set(data.get("ids", []))
    ids.add(news_id)
    titles = data.get("titles", [])
    titles.append(title[:120])
    save_json(POSTED_FILE, {
        "ids": list(ids)[-1000:],
        "titles": titles[-RECENT_TITLES_MEMORY:],
    })


def make_news_id(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:16]


# ============================================================
# СБОР НОВОСТЕЙ + ДИАГНОСТИКА
# ============================================================

def fetch_feed(url: str):
    """RSS с User-Agent (Reddit и часть сайтов иначе блокируют)."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TelegramAINewsBot/1.0"
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            return feedparser.parse(resp.read())
    except Exception:
        try:
            return feedparser.parse(url)
        except Exception:
            return None


def entry_age_hours(entry) -> float | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                dt = datetime(*t[:6], tzinfo=timezone.utc)
                return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            except Exception:
                continue
    return None


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()


# ============================================================
# ОЧИСТКА ПОСТА ОТ СЛУЖЕБНОЙ БОЛТОВНИ МОДЕЛИ
# ============================================================
# Claude иногда предваряет пост фразами вроде "Вот пост для канала:"
# или "Отлично, у меня есть свежая информация. Составим пост:".
# Такое сразу выдаёт, что писал не человек. Чистим в три слоя:
# теги <post>, срез служебных строк, отбраковка мусора.

POST_TAG_RE = re.compile(r"<post>(.*?)</post>", re.S | re.I)
FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$", re.M)

META_WORDS = (
    "пост", "текст", "вариант", "напишу", "напишем", "составим", "составлю",
    "информаци", "поиск", "нашёл", "нашел", "данные", "факт", "готов",
    "давайте", "давай", "итак", "теперь", "оформлю", "сделаю", "версия",
)
FILLER_START = (
    "вот", "отлично", "ладно", "хорошо", "супер", "понял", "окей", "ок,",
    "итак", "теперь", "готово", "прекрасно", "замечательно", "здорово",
)
TAIL_META = (
    "хочешь", "если нужно", "если хочешь", "могу также", "могу ещё",
    "могу еще", "дай знать", "дайте знать", "нужно ли", "подойдёт",
    "подойдет", "могу переписать", "могу сократить",
)


def _is_meta_line(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 220:
        return False
    if "<" in s and ">" in s:      # строка с разметкой — это уже сам пост
        return False
    low = s.lower()
    starts_filler = any(low.startswith(w) for w in FILLER_START)
    has_meta = any(w in low for w in META_WORDS)
    ends_colon = s.rstrip().endswith(":")
    return (ends_colon and has_meta) or (starts_filler and has_meta)


def clean_post(text: str | None) -> str | None:
    """Возвращает чистый текст поста или None, если чистить нечего."""
    if not text:
        return None
    t = text.strip()

    m = POST_TAG_RE.search(t)
    if m:
        t = m.group(1).strip()

    t = FENCE_RE.sub("", t).strip()

    lines = t.split("\n")
    removed = []
    for _ in range(4):
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines and _is_meta_line(lines[0]):
            removed.append(lines.pop(0).strip())
        else:
            break

    while lines:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and any(lines[-1].strip().lower().startswith(w) for w in TAIL_META):
            removed.append(lines.pop().strip())
        else:
            break

    if removed:
        logger.info(f"🧹 Срезана болтовня: {removed}")

    t = "\n".join(lines).strip()
    if len(t) < 80:
        logger.warning("После чистки почти ничего не осталось — отбраковка")
        return None
    return t


def fetch_news() -> tuple[list[dict], dict]:
    """Возвращает (новости, диагностика)."""
    posted_ids = set(load_posted().get("ids", []))
    all_news = []
    diag = {"dead_sources": [], "dropped_old": 0, "dropped_undated": 0, "by_group": {}}

    for group_name, cfg in SOURCE_GROUPS.items():
        group_count = 0
        for source_name, rss_url in cfg["feeds"]:
            feed = fetch_feed(rss_url)
            if not feed or not getattr(feed, "entries", None):
                diag["dead_sources"].append(source_name)
                continue

            taken = 0
            for entry in feed.entries:
                if taken >= cfg["take"]:
                    break
                url = entry.get("link", "")
                if not url:
                    continue
                news_id = make_news_id(url)
                if news_id in posted_ids:
                    continue

                age = entry_age_hours(entry)
                if age is None:
                    if DROP_UNDATED:
                        diag["dropped_undated"] += 1
                        continue
                elif age > FRESHNESS_HOURS:
                    diag["dropped_old"] += 1
                    continue

                summary = strip_html(entry.get("summary", "") or entry.get("description", ""))
                all_news.append({
                    "id": news_id,
                    "title": entry.get("title", "").strip(),
                    "summary": summary[:900],
                    "url": url,
                    "source": source_name,
                    "group": group_name,
                    "age_h": round(age, 1) if age is not None else None,
                })
                taken += 1
                group_count += 1
        diag["by_group"][group_name] = group_count

    return all_news, diag


# ============================================================
# ДЕТЕКТ "УЖЕ У ВСЕХ" (кросс-источниковое перекрытие)
# ============================================================

STOPWORDS = {
    "this", "that", "with", "from", "have", "will", "your", "just", "about",
    "into", "over", "they", "their", "what", "when", "which", "than", "been",
    "новый", "новая", "новое", "может", "было", "если", "как", "что", "для",
    "the", "and", "for", "you", "now", "not", "但是", "показал",
}


def title_tokens(title: str) -> set:
    words = re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]{4,}", (title or "").lower())
    return {w for w in words if w not in STOPWORDS}


def mark_coverage(news_list: list[dict]) -> list[dict]:
    """Считает, в скольких РАЗНЫХ источниках всплыла та же история."""
    tokens = [title_tokens(n["title"]) for n in news_list]
    for i, n in enumerate(news_list):
        sources = {n["source"]}
        for j, other in enumerate(news_list):
            if i == j or not tokens[i] or not tokens[j]:
                continue
            inter = len(tokens[i] & tokens[j])
            union = len(tokens[i] | tokens[j])
            if union and inter / union >= 0.35:
                sources.add(other["source"])
        n["coverage"] = len(sources)
        n["everywhere"] = len(sources) >= EVERYWHERE_THRESHOLD
    return news_list


# ============================================================
# ОЦЕНКА И ВЫБОР ТЕМЫ
# ============================================================

SELECTOR_PROMPT = """Ты — редактор русскоязычного Telegram-канала для AI-креаторов (делают видео, музыку, арт через нейросети). Аудитория — практики.

Тебе дан список кандидатов. Оцени каждого мысленно по трём шкалам 0-2:
1. ПЕРЕСЫЛАЕМОСТЬ — захочется ли скинуть другу
2. МОЖНО ПОПРОБОВАТЬ РУКАМИ — есть ли что пойти и сделать сегодня (максимальный вес!)
3. СВЕЖЕСТЬ — чем меньше часов, тем лучше

ЖЁСТКИЕ ПРАВИЛА:
- У кандидатов помечено "покрытие: N источников". Если N большое — про это уже написали все каналы, ценность низкая. Выбирай такое ТОЛЬКО если это действительно крупное событие для креаторов и добавить нечего другого.
- Тема, которую заметил один источник, но она полезна практику — лучший выбор.
- Тебе дан список недавно опубликованных тем. НЕ бери тематически похожее.
- Избегай: корпоративные драмы, инвестиции, политика, регуляции, чистая наука без применения.
- Приоритет: генеративные модели (видео/аудио/изображения), инструменты креаторов, open-source находки, приёмы работы.

Ответь СТРОГО в JSON без пояснений:
{"index": <номер лучшего кандидата>, "why": "<до 10 слов почему>"}"""


def select_best_news(news_list: list[dict]) -> dict | None:
    if not news_list:
        return None
    if len(news_list) == 1:
        return news_list[0]
    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        recent = load_posted().get("titles", [])
        recent_block = "\n".join(f"- {t}" for t in recent) if recent else "(пусто)"
        cands = "\n\n".join([
            f"[{i+1}] ({n['group']} / {n['source']}, {n['age_h']}ч, покрытие: {n['coverage']} источн.)\n"
            f"{n['title']}\n{n['summary'][:220]}"
            for i, n in enumerate(news_list)
        ])
        msg = f"НЕДАВНО ПУБЛИКОВАЛИ (не повторяться):\n{recent_block}\n\nКАНДИДАТЫ:\n\n{cands}"
        resp = client.messages.create(
            model=MODEL, max_tokens=120,
            system=SELECTOR_PROMPT,
            messages=[{"role": "user", "content": msg}],
        )
        raw = resp.content[0].text.strip()
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            data = json.loads(m.group(0))
            idx = int(data.get("index", 1)) - 1
            if 0 <= idx < len(news_list):
                chosen = news_list[idx]
                logger.info(f"✅ Выбрано #{idx+1} ({chosen['group']}): {chosen['title'][:60]}")
                logger.info(f"   Причина: {data.get('why', '-')} | покрытие: {chosen['coverage']}")
                return chosen
        # запасной путь: минимальное покрытие + свежесть
        return sorted(news_list, key=lambda n: (n["coverage"], n["age_h"] or 99))[0]
    except Exception as e:
        logger.error(f"Ошибка выбора: {e}")
        return sorted(news_list, key=lambda n: (n.get("coverage", 9), n.get("age_h") or 99))[0]


# ============================================================
# НАПИСАНИЕ ПОСТОВ
# ============================================================

STYLE_RULES = """СТИЛЬ: живой, от практика, лёгкая дерзость без кринжа. Конкретика вместо воды.
ЗАПРЕЩЕНО: «компания объявила», «стоит отметить», «в современном мире», канцелярит, выдуманные факты.
HTML: только <b>, <i>, <code>. Промпты и параметры — в <code>.
Не добавляй ссылки, источники и хэштеги — добавятся автоматически.

ФОРМАТ ОТВЕТА — КРИТИЧНО:
Готовый пост оберни в теги <post> и </post>. Внутри — только сам текст поста.
Запрещено писать перед тегом <post> или внутри него любые обращения ко мне
и комментарии о своей работе. Никаких «Вот пост», «Отлично, я нашёл информацию»,
«Составим пост», «Напишу пост с фактами», «Готово». Канал читают люди —
любая такая фраза сразу выдаёт, что текст писала нейросеть.
Первый символ внутри <post> — это первый символ поста."""

NEWS_WRITER_PROMPT = f"""Ты — автор Telegram-канала про AI для создателей контента.

СТРУКТУРА:
1. Заголовок жирным с эмодзи в начале
2. Одна вводная строка — почему это важно
3. 3-5 пунктов через тире с <b>ключевым словом</b> — конкретика, цифры
4. Блок «что с этим делать» — практический шаг для читателя
5. Финал: вывод, прогноз или вопрос

ДЛИНА: до 900 символов.
{STYLE_RULES}"""


def write_news_post(news: dict) -> str | None:
    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = (f"Заголовок: {news['title']}\nИсточник: {news['source']}\n"
               f"Содержание: {news['summary']}\n\nТолько факты из новости, ничего не выдумывай.")
        resp = client.messages.create(
            model=MODEL, max_tokens=1200,
            system=NEWS_WRITER_PROMPT,
            messages=[{"role": "user", "content": msg}],
        )
        return clean_post(resp.content[0].text)
    except Exception as e:
        logger.error(f"Ошибка написания новости: {e}")
        return None


# ---------- Тема из topics.txt с веб-поиском ----------

def load_topics() -> list[str]:
    if not TOPICS_FILE.exists():
        return []
    lines = TOPICS_FILE.read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def pick_topic() -> str | None:
    topics = load_topics()
    if not topics:
        return None
    used_data = load_json(USED_TOPICS_FILE, {})
    used = set(used_data.get("dynamic_used", []))
    available = [t for t in topics if t not in used]
    if not available:
        logger.info("♻️ Круг тем пройден — начинаю заново")
        used_data["dynamic_used"] = []
        save_json(USED_TOPICS_FILE, used_data)
        available = topics
    return random.choice(available)


def mark_topic_used(topic: str):
    d = load_json(USED_TOPICS_FILE, {})
    d.setdefault("dynamic_used", []).append(topic)
    save_json(USED_TOPICS_FILE, d)


TOPIC_WRITER_PROMPT = f"""Ты — автор русскоязычного Telegram-канала для AI-креаторов. Аудитория — практики, не новички.

ЗАДАЧА: по заданной теме найди через веб-поиск СВЕЖУЮ информацию (актуальные версии, цены, фишки) и напиши пост.
Опирайся только на найденное. Не выдумывай версии и цифры. При противоречиях бери более свежий источник.

СТРУКТУРА:
1. Крюк — цепляет и обещает пользу
2. <b>Заголовок темы</b>
3. Суть — конкретные факты из поиска
4. Что с этим делать читателю
5. Финальная фраза или вопрос

ДЛИНА: 600-900 символов (для промптов/инструкций — до 1100).
{STYLE_RULES}"""


def write_topic_post(topic: str) -> str | None:
    return _write_with_search(
        system=TOPIC_WRITER_PROMPT,
        user=f"Тема поста: {topic}\n\nНайди свежую информацию и напиши пост.",
    )


# ---------- Охота за тихими изменениями ----------

QUIET_CHANGES_PROMPT = f"""Ты — автор русскоязычного Telegram-канала для AI-креаторов.

ЗАДАЧА: про новые модели пишут все. Твоя тема — ТИХИЕ ИЗМЕНЕНИЯ, которые касаются людей сильнее релизов:
смена цен и тарифов, изменение лимитов генераций, доступ к функциям в подписках, изменения бесплатных планов,
закрытие или открытие доступа, смена условий лицензий и коммерческого использования.

Через веб-поиск найди 2-4 ТАКИХ изменения за последние дни у сервисов для креаторов
(видео-, аудио-, изображение-генераторы, голосовые сервисы, платформы генерации).

ПРАВИЛА:
- Только изменения, подтверждённые найденными источниками. Не выдумывай цифры и даты.
- Если нашёл меньше двух — пиши про то, что нашёл, не добирай выдумками.
- Указывай было/стало, если известно.

СТРУКТУРА:
1. Заголовок жирным с эмодзи — про то, что все смотрят на релизы, а деньги решают детали
2. Каждое изменение отдельным пунктом: <b>сервис</b> — что поменялось
3. Практический вывод: кому и что пересчитать/проверить
4. Финальная фраза

ДЛИНА: до 900 символов.
{STYLE_RULES}"""


def write_quiet_changes_post() -> str | None:
    return _write_with_search(
        system=QUIET_CHANGES_PROMPT,
        user="Найди свежие изменения цен, лимитов и доступа у AI-сервисов для креаторов и напиши пост.",
    )


def _write_with_search(system: str, user: str) -> str | None:
    """Общий вызов Claude с веб-поиском (обрабатывает pause_turn)."""
    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        messages = [{"role": "user", "content": user}]
        tools = [{"type": "web_search_20250305", "name": "web_search",
                  "max_uses": WEB_SEARCH_MAX_USES}]
        resp = None
        for _ in range(4):
            resp = client.messages.create(
                model=MODEL, max_tokens=2000,
                system=system, messages=messages, tools=tools,
            )
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            break
        # Между поисками модель комментирует свои действия отдельными
        # текстовыми блоками. Пост — всегда в ПОСЛЕДНЕМ блоке, поэтому
        # склеивать все нельзя: болтовня попадёт в канал.
        text_blocks = [b.text for b in resp.content
                       if getattr(b, "type", "") == "text" and b.text.strip()]
        if not text_blocks:
            logger.warning("Модель не вернула текст")
            return None
        if len(text_blocks) > 1:
            logger.info(f"🧹 Отброшено служебных блоков: {len(text_blocks) - 1}")

        # Если пост обёрнут в теги — ищем блок с ними, иначе берём последний
        tagged = [b for b in text_blocks if "<post>" in b.lower()]
        raw = tagged[-1] if tagged else text_blocks[-1]

        text = clean_post(raw)
        if not text:
            return None
        logger.info(f"✏️ Пост с поиском: {len(text)} символов")
        return text
    except Exception as e:
        logger.error(f"Ошибка поста с поиском: {e}")
        return None


# ============================================================
# ПУБЛИКАЦИЯ
# ============================================================

async def publish(text: str, source_url: str | None = None,
                  source_name: str | None = None) -> bool:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    parts = [text]
    if source_url and source_name:
        parts.append(f"\n🔗 <a href=\"{source_url}\">{source_name}</a>")
    parts.append("\n#нейросети #AI #ИИ")
    full = "\n".join(parts)
    try:
        await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=full,
                               parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        logger.info("✅ Опубликовано")
        return True
    except Exception as e:
        logger.error(f"Ошибка публикации: {e}")
        try:
            await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=strip_html(full))
            logger.info("✅ Опубликовано без разметки")
            return True
        except Exception as e2:
            logger.error(f"И без разметки не вышло: {e2}")
            return False


# ============================================================
# РЕЖИМЫ
# ============================================================

async def do_news() -> bool:
    news_list, diag = fetch_news()

    # ДИАГНОСТИКА — видно в логах GitHub Actions
    logger.info("── Диагностика сбора ──")
    logger.info(f"   Собрано: {len(news_list)} | по группам: {diag['by_group']}")
    logger.info(f"   Отсеяно по возрасту (>{FRESHNESS_HOURS}ч): {diag['dropped_old']}")
    logger.info(f"   Отсеяно без даты: {diag['dropped_undated']}")
    if diag["dead_sources"]:
        logger.warning(f"   Не ответили: {', '.join(diag['dead_sources'])}")

    if not news_list:
        return False

    news_list = mark_coverage(news_list)
    everywhere = sum(1 for n in news_list if n["everywhere"])
    logger.info(f"   Помечено «уже у всех»: {everywhere}")

    # Сначала редкие темы, потом свежие — так селектор видит недооценённое сверху
    news_list.sort(key=lambda n: (n["coverage"], n["age_h"] or 99))

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


async def do_topic() -> bool:
    topic = pick_topic()
    if not topic:
        return False
    logger.info(f"🎯 Тема дня: {topic}")
    text = write_topic_post(topic)
    if not text:
        return False
    ok = await publish(text)
    if ok:
        mark_topic_used(topic)
        remember_title(topic)
    return ok


async def do_quiet_changes() -> bool:
    logger.info("🔍 Режим: охота за тихими изменениями")
    text = write_quiet_changes_post()
    if not text:
        return False
    ok = await publish(text)
    if ok:
        remember_title("тихие изменения цен и лимитов")
    return ok


# ============================================================
# MAIN
# ============================================================

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

    # Расписание: каждый 6-й день — тихие изменения,
    # чётные — новость, нечётные — тема из topics.txt
    if day % 6 == 0:
        order = [do_quiet_changes, do_news, do_topic]
    elif day % 2 == 0:
        order = [do_news, do_topic, do_quiet_changes]
    else:
        order = [do_topic, do_news, do_quiet_changes]

    logger.info(f"📅 День {day} → основной режим: {order[0].__name__}")

    for fn in order:
        if await fn():
            return
        logger.info(f"{fn.__name__} не сработал — пробую следующий режим")
    logger.error("❌ Ни один режим не дал поста")


if __name__ == "__main__":
    asyncio.run(main())
