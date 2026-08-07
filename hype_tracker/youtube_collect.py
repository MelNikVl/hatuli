#!/usr/bin/env python3
"""YouTube как источник хайпа (см. задачу "тепловая карта хайпа", п.4 —
5 каналов о недвижимости Астаны). YouTube's RSS-эндпоинт
(youtube.com/feeds/videos.xml) отдаёт 404 с этого сервера (похоже на
сетевые ограничения хостера — тот же класс проблемы, что с Overpass-
зеркалами, см. bot/score_layers/osm.py), а текущая вёрстка страницы канала
(lockupViewModel) не парсится простым regex, как раньше (videoRenderer) —
поэтому используем yt-dlp (венду free, без ключа API, отдельно pip install
yt-dlp) вместо самодельного скрейпинга.

Пишет новые видео (title + description) в ту же таблицу news (krisha_bot),
что и news_collect.py — просто ещё один источник, который дальше сам
подхватит news_analyze.py (тот же LLM-пайплайн определения ЖК/локации +
sentiment, никакого дублирования логики). Транскрипты (субтитры) не
качаем — заголовки риелторских обзоров и так почти всегда содержат
название ЖК и оценочные слова ("лучший", "не советую" и т.п.) — этого
достаточно LLM для relevant/sentiment, а текст видео добавил бы точности
не пропорционально сложности (плюс не у всех роликов вообще есть субтитры).

Запуск: venv/bin/python3 youtube_collect.py
Требует: venv/bin/pip install yt-dlp
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras

BASE = Path("/home/nik/krisha_bot")

# 5 каналов из задачи — handle это то, что после youtube.com/@.
# ПРОВЕРЕНО вручную (curl + externalId в HTML страницы канала), реальные:
CHANNELS = [
    ("Krisha.kz (YouTube)", "KrishaKZvideo"),
    ("Окна Столицы", "oknastolicy01"),
]
# НЕ добавлены — не смог надёжно найти реальный @handle без угадывания
# (риск утянуть чужой/неактивный канал и засорить хайп-пайплайн мусором):
#   - "Zhadyra / Smarent" — нашёл только "Smarent Pro недвижимость"
#     (основатель Виктор Зубик), имя "Zhadyra" нигде не подтвердилось —
#     возможно это соведущая или другой канал, уточни у пользователя.
#   - "Avenue Real Estate" — не нашёл отдельного канала с таким названием,
#     только чужие обзоры ЖК от BI Group (Avenue 5 и т.п.) на разных каналах.
#   - "Анар Бибосинова" — не нашёл YouTube вообще, похоже её контент
#     в основном в Instagram/TikTok, не на YouTube.
# Добавь сюда ("Название", "handle") после проверки — формула проверки:
#   curl -sL "https://www.youtube.com/@ИМЯ" -A "Mozilla/5.0 ..." | \
#     grep -o '"externalId":"[^"]*"'
# — если находит непустой externalId, канал реальный.
VIDEOS_PER_CHANNEL = 8  # сколько последних видео проверяем за прогон


def load_database_url() -> str:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return "postgresql://krisha@localhost/krisha_bot"


def conn():
    return psycopg2.connect(load_database_url())


def list_recent_videos(handle: str, limit: int) -> list[dict]:
    """flat-extract — быстро, без захода на каждое видео (только то, что
    есть в самом листинге канала: id, title)."""
    import yt_dlp
    opts = {
        "extract_flat": True, "playlistend": limit, "quiet": True,
        "no_warnings": True, "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/@{handle}/videos", download=False)
    return info.get("entries", []) or []


def fetch_description(video_id: str) -> str | None:
    """Полные метаданные ОДНОГО видео (не flat) — только для новых видео,
    чтобы не грузить yt-dlp запросами на весь листинг каждый раз."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        desc = (info.get("description") or "").strip()
        return desc[:600] if desc else None
    except Exception as e:
        print(f"# fetch_description({video_id}) failed: {e}", file=sys.stderr)
        return None


def main() -> None:
    db = conn()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT url FROM news WHERE ts > now() - interval '60 days'")
    seen_urls = {r["url"] for r in cur.fetchall()}

    total_new = 0
    for source_name, handle in CHANNELS:
        try:
            entries = list_recent_videos(handle, VIDEOS_PER_CHANNEL)
        except Exception as e:
            print(f"# channel {handle} failed: {e}", file=sys.stderr)
            continue
        new_here = 0
        for e in entries:
            vid = e.get("id")
            if not vid:
                continue
            url = f"https://www.youtube.com/watch?v={vid}"
            if url in seen_urls:
                continue
            title = (e.get("title") or "").strip()
            if not title:
                continue
            summary = fetch_description(vid)
            time.sleep(1.5)
            thumb = e.get("thumbnails", [{}])[-1].get("url") if e.get("thumbnails") else None
            try:
                cur.execute(
                    "INSERT INTO news (title, source, url, image_url, summary) VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT (url) DO NOTHING",
                    (title[:500], source_name, url, thumb, summary))
                db.commit()
                new_here += 1
                total_new += 1
                seen_urls.add(url)
            except Exception as ex:
                print(f"# insert error {url}: {ex}", file=sys.stderr)
        print(f"{source_name} (@{handle}): новых видео = {new_here}")
        time.sleep(2)

    db.close()
    print(f"готово: новых видео всего = {total_new}")


if __name__ == "__main__":
    main()
