#!/usr/bin/env python3
"""Автоматический анализ новостей -> hype_locations (ежедневный пайплайн).

Раньше "хайп по новостям" на карте (/admin/analytics/hype) заполнялся
вручную: человек читал новости, руками писал JSON и запускал
location_upsert.py. Этот скрипт делает то же самое автоматически:

1. Тянет свежие RSS-новости теми же запросами/парсером, что и
   news_collect.py (переиспользуем QUERIES/get_rss/parse_rss оттуда),
   льёт их в таблицу news (krisha_bot) как обычно.
2. Берёт новости за последние 10 дней, которых ещё не было в
   hype_tracker.processed_articles (новая табличка — trqacking по url,
   т.к. news лежит в другой БД и джойнить напрямую нельзя).
3. Для каждой непройденной новости шлёт заголовок+summary в DeepSeek
   (тот же HTTP-контракт, что и bot/core/ai_text_analysis.py, но синхронный
   requests, т.к. этот скрипт вне async-приложения) и просит определить:
   упомянут ли конкретный ЖК/локация/станция ЛРТ, рейтинг хайпа 0-100,
   причину.
4. Пытается геопривязать результат:
   - если упомянута станция ЛРТ (krisha_bot.city_poi, kind='landmark',
     name LIKE 'ЛРТ:%') — берёт все НЕ-мусорные ЖК (complexes,
     is_garbage=false) в радиусе 500м и хайпует их (рейтинг чуть падает
     с расстоянием) — так осмысленнее для карты, чем один маркер в
     чистом поле на месте станции;
   - иначе пытается найти ЖК по имени в complexes (ILIKE-совпадение
     в обе стороны);
   - если ничего не нашлось — новость логируется и пропускается (без
     lat/lon точка всё равно не попадёт на карту, см.
     /admin/api/hype-locations: WHERE lat IS NOT NULL).
5. Апсертит найденные локации в hype_tracker.hype_locations +
   hype_location_history через location_upsert.upsert_location() —
   ОДНА логика апсерта и для ручного, и для автоматического пути.

Флаг: AI_HYPE_NEWS в app_settings (krisha_bot), по умолчанию включён.
Требует DEEPSEEK_API_KEY в .env — если его нет, скрипт громко
завершается, ничего не выдумывая.

Запуск: venv/bin/python hype_tracker/news_analyze.py
Расписание: krisha-hype-news.timer (ежедневно, см. systemd unit).
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests

sys.path.insert(0, str(Path(__file__).parent))
import news_collect  # noqa: E402  (переиспользуем RSS-фетчер)
import location_upsert  # noqa: E402  (переиспользуем апсерт)

BASE = Path("/home/nik/krisha_bot")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
ARTICLE_WINDOW_DAYS = 10
# Поднято с 40 — news_collect.py теперь тянет до 100 статей/день с
# поимёнными запросами по ~80 ЖК (было 4 общих запроса) — 40/прогон душило
# бы именно новый, самый ценный поток.
MAX_ARTICLES_PER_RUN = 80
LRT_RADIUS_M = 500
PARK_RADIUS_M = 700  # парк больше станции ЛРТ — радиус щедрее

SYSTEM_PROMPT = """Ты аналитик рынка недвижимости Астаны (Казахстан). Тебе дают заголовок
и краткое содержание новости. Определи, упоминается ли в ней КОНКРЕТНЫЙ жилой
комплекс (ЖК), район, локация или инфраструктурный объект, связанный с недвижимостью
(например: "рядом со станцией ЛРТ Есиль", "в районе Есиль", открытие новой ЖК,
крупный инфраструктурный проект рядом с конкретным местом и т.п.).
Верни ТОЛЬКО валидный JSON без markdown и пояснений, со схемой:
{
 "relevant": true/false,
 "name": "название ЖК/локации как упомянуто в новости, или null",
 "district": "район, если можно определить, или null",
 "lrt_station": "название станции ЛРТ без слова ЛРТ (например 'Есиль'), если упомянута, иначе null",
 "park": "название парка/сквера/зелёной зоны, если он упомянут В ПОЛОЖИТЕЛЬНОМ КОНТЕКСТЕ (открытие, благоустройство, реконструкция, новая зона отдыха, озеленение) и это не ТРЦ/торговый центр; иначе null",
 "rating": 0-100,
 "sentiment": -1.0..1.0,
 "reason": "одно предложение — почему это важно, с опорой на факт из новости"
}
rating: открытие метро/ЛРТ рядом, крупный инфраструктурный проект, запуск/сдача
крупного ЖК = высокий (70-100); умеренная новость по локации = средний (40-69);
небольшое/косвенное упоминание = низкий (10-39). Если новость вообще не про
конкретную недвижимость/локацию (общие законы, ипотечная статистика без привязки
к месту и т.п.) — relevant=false и остальные поля null/0.
sentiment — тональность самой новости про этот ЖК/локацию, НЕ связана с
rating (важность/интенсивность) — высокий rating бывает и у скандала:
  +0.5..+1.0 — позитив (старт продаж, сдача, открытие, раскупили, ажиотаж)
  -0.2..+0.2 — нейтрально (просто факт/статистика без явной окраски)
  -1.0..-0.5 — негатив (долгострой, задержка сдачи, обманутые дольщики,
    проблемный застройщик, судебные иски, жалобы дольщиков)
Не выдумывай факты и названия, которых нет в тексте."""


def load_database_url() -> str:
    return news_collect.load_database_url()


def load_env_key(key: str) -> str | None:
    """Читаем ключ из .env напрямую (как load_database_url) — это
    отдельный синхронный скрипт вне async-приложения, os.environ .env
    ему автоматически никто не подгружает."""
    val = os.getenv(key)
    if val:
        return val
    try:
        for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def main_conn():
    """krisha_bot: news, complexes, city_poi, app_settings."""
    return psycopg2.connect(load_database_url())


def hype_conn():
    """hype_tracker: processed_articles, hype_locations, hype_location_history."""
    return psycopg2.connect(load_database_url().rsplit("/", 1)[0] + "/hype_tracker")


def is_enabled() -> bool:
    """Флаг AI_HYPE_NEWS в app_settings (по умолчанию включён — True).
    Скрипт не часть async-приложения, общего app_settings-кеша
    (bot/db/settings.py) у него нет — читаем напрямую через psycopg2,
    как и остальные синхронные скрипты в hype_tracker/."""
    try:
        conn = main_conn()
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key = 'AI_HYPE_NEWS'")
        row = cur.fetchone()
        conn.close()
        if row is None:
            return True
        return str(row[0]).strip() in ("1", "true", "True", "on")
    except Exception as e:
        print(f"[WARN] не удалось прочитать AI_HYPE_NEWS, считаю включённым: {e}", flush=True)
        return True


def ensure_processed_table(hcur) -> None:
    hcur.execute("""
        CREATE TABLE IF NOT EXISTS processed_articles (
            url TEXT PRIMARY KEY,
            processed_at TIMESTAMPTZ DEFAULT now()
        )
    """)


def fetch_new_articles() -> int:
    """Тянем RSS + Krisha.kz editorial-раздел, льём в news (krisha_bot).
    Без картинок/Playwright — нам нужен только текст для LLM-анализа, а не
    витрина для UI (это уже отдельная забота news_collect.py, если/когда
    его тоже поставят на крон).

    ВАЖНО (было раньше): использовался news_collect.QUERIES — статичный
    список БЕЗ поимённых ЖК-запросов и без Krisha content (они добавлены
    в news_collect.load_queries()/fetch_krisha_content() отдельно, но
    news_analyze.py их не подхватывал — единственный реально работающий по
    крону путь (krisha-hype-news.timer, 06:30 ежедневно) продолжал бы
    получать только 4 общих запроса). Переключено на load_queries() +
    добавлен вызов fetch_krisha_content() — то же самое, что теперь тянет
    news_collect.py при ручном запуске."""
    conn = main_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT url FROM news WHERE ts > now() - interval '30 days'")
    seen = {r["url"] for r in cur.fetchall()}

    new_items = []
    for path in news_collect.KRISHA_CONTENT_PATHS:
        try:
            for it in news_collect.fetch_krisha_content(path):
                if it["url"] in seen or it["url"] in [x["url"] for x in new_items]:
                    continue
                new_items.append(it)
            time.sleep(2)
        except Exception as e:
            print(f"[WARN] krisha content error {path}: {e}", flush=True)

    for q in news_collect.load_queries():
        try:
            for it in news_collect.parse_rss(news_collect.get_rss(q)):
                if it["url"] in seen or it["url"] in [x["url"] for x in new_items]:
                    continue
                new_items.append(it)
            time.sleep(1)
        except Exception as e:
            print(f"[WARN] rss error {q}: {e}", flush=True)

    for it in new_items:
        try:
            # summary — Krisha content отдаёт готовый анонс прямо со
            # страницы листинга (см. fetch_krisha_content), RSS-статьи —
            # без него (там summary дозаполняется отдельным Playwright-шагом
            # в news_collect.py, который сюда не портирован, не наша забота
            # здесь: LLM работает и по title+summary=None).
            cur.execute(
                "INSERT INTO news (title, source, url, summary) VALUES (%s,%s,%s,%s) ON CONFLICT (url) DO NOTHING",
                (it["title"][:500], it["source"][:100], it["url"], (it.get("summary") or None)))
        except Exception as e:
            print(f"[WARN] insert error: {e}", flush=True)

    conn.commit()
    conn.close()
    print(f"новых статей найдено: {len(new_items)}", flush=True)
    return len(new_items)


def call_deepseek(api_key: str, title: str, summary: str | None) -> dict | None:
    text = f"Заголовок: {title}\n\nКратко: {summary or '(нет описания, только заголовок)'}"
    try:
        resp = requests.post(
            DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.1,
                "max_tokens": 400,
            },
            timeout=60,
        )
    except Exception as e:
        print(f"[WARN] deepseek запрос упал: {e}", flush=True)
        return None
    if resp.status_code != 200:
        print(f"[WARN] deepseek {resp.status_code}: {resp.text[:200]}", flush=True)
        return None
    content = resp.json()["choices"][0]["message"]["content"].strip()
    if content.startswith("```json"):
        content = content[len("```json"):]
    elif content.startswith("```"):
        content = content[len("```"):]
    if content.endswith("```"):
        content = content[:-3]
    content = content.strip()
    try:
        return json.loads(content)
    except Exception as e:
        print(f"[WARN] не смог распарсить JSON от deepseek: {e} -- {content[:200]}", flush=True)
        return None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_lrt_station(mcur, station_name: str):
    mcur.execute(
        "SELECT name, lat, lon FROM city_poi WHERE kind='landmark' AND name LIKE 'ЛРТ:%%' AND name ILIKE %s LIMIT 1",
        (f"%{station_name}%",))
    return mcur.fetchone()


def find_park(mcur, park_name: str):
    """Парк/сквер в city_poi по названию. Исключаем ТРЦ (Азия Парк, MEGA Park и т.п.)."""
    mcur.execute(
        "SELECT name, lat, lon FROM city_poi "
        "WHERE kind='landmark' AND lat IS NOT NULL AND lon IS NOT NULL "
        "  AND (name ILIKE %s OR %s ILIKE ('%%' || name || '%%')) "
        "  AND name NOT ILIKE '%%трц%%' AND name NOT ILIKE '%%торговый%%' "
        "  AND name NOT ILIKE '%%тц %%' AND name NOT ILIKE '%%тц%%центр%%' "
        "  AND (name ILIKE '%%парк%%' OR name ILIKE '%%сквер%%' OR name ILIKE '%%бульвар%%' "
        "       OR name ILIKE '%%сад%%' OR name ILIKE '%%зелен%%') "
        "ORDER BY length(name) DESC LIMIT 1",
        (f"%{park_name}%", park_name))
    return mcur.fetchone()


def find_complex_match(mcur, name: str):
    mcur.execute(
        """SELECT name, district, lat, lon FROM complexes
           WHERE is_garbage IS NOT TRUE AND lat IS NOT NULL AND lon IS NOT NULL
             AND (name ILIKE %s OR %s ILIKE ('%%' || name || '%%'))
           ORDER BY length(name) DESC LIMIT 1""",
        (f"%{name}%", name))
    return mcur.fetchone()


def nearby_complexes(mcur, lat: float, lon: float, radius_m: float = LRT_RADIUS_M, limit: int = 10):
    mcur.execute(
        "SELECT name, district, lat, lon FROM complexes WHERE is_garbage IS NOT TRUE AND lat IS NOT NULL AND lon IS NOT NULL")
    out = []
    for row in mcur.fetchall():
        d = haversine_m(lat, lon, row["lat"], row["lon"])
        if d <= radius_m:
            out.append((row, d))
    out.sort(key=lambda x: x[1])
    return out[:limit]


def main() -> None:
    if not is_enabled():
        print("AI_HYPE_NEWS выключен в app_settings — выхожу без обработки.", flush=True)
        return

    api_key = load_env_key("DEEPSEEK_API_KEY")
    if not api_key:
        print("[ERROR] DEEPSEEK_API_KEY не задан в .env — анализ новостей невозможен без LLM, выхожу.", flush=True)
        return

    try:
        fetch_new_articles()
    except Exception as e:
        print(f"[WARN] fetch_new_articles упал: {e}", flush=True)

    # YouTube (см. hype_tracker/youtube_collect.py, задача "тепловая карта
    # хайпа" п.4) — те же 5 (пока 2 подтверждённых) каналов о недвижимости,
    # видео льются в ту же news, дальше обрабатываются тем же LLM-циклом
    # ниже наравне со статьями. Отдельный процесс (yt-dlp тяжелее urllib),
    # падение не должно рушить остальной анализ.
    try:
        import subprocess
        subprocess.run(
            [sys.executable, str(BASE / "hype_tracker" / "youtube_collect.py")],
            timeout=600, check=False)
    except Exception as e:
        print(f"[WARN] youtube_collect упал: {e}", flush=True)

    mconn = main_conn()
    hconn = hype_conn()
    mcur = mconn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    hcur = hconn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    ensure_processed_table(hcur)
    hconn.commit()

    hcur.execute("SELECT url FROM processed_articles")
    processed = {r["url"] for r in hcur.fetchall()}

    mcur.execute(
        "SELECT id, title, url, summary FROM news WHERE ts > now() - interval %s ORDER BY ts DESC",
        (f"{ARTICLE_WINDOW_DAYS} days",))
    articles = [a for a in mcur.fetchall() if a["url"] not in processed][:MAX_ARTICLES_PER_RUN]
    print(f"статей к анализу: {len(articles)}", flush=True)

    n_analyzed = 0
    n_relevant = 0
    n_geocoded = 0
    n_upserts = 0
    n_skipped_no_geo = 0
    n_llm_failed = 0

    for art in articles:
        result = call_deepseek(api_key, art["title"], art["summary"])
        n_analyzed += 1
        if result is None:
            n_llm_failed += 1
            # LLM/сеть подвели — НЕ отмечаем как processed, попробуем эту статью в след. прогоне
            time.sleep(1)
            continue

        # успешно проанализировано (даже если relevant=false) — не будем спрашивать LLM про неё снова
        hcur.execute(
            "INSERT INTO processed_articles (url) VALUES (%s) ON CONFLICT (url) DO NOTHING",
            (art["url"],))
        hconn.commit()

        if not result.get("relevant"):
            time.sleep(1)
            continue
        n_relevant += 1

        name = (result.get("name") or "").strip()
        rating = float(result.get("rating") or 0)
        reason = (result.get("reason") or "").strip()
        district = result.get("district") or None
        try:
            sentiment = max(-1.0, min(1.0, float(result.get("sentiment"))))
        except (TypeError, ValueError):
            sentiment = None
        lrt_station = (result.get("lrt_station") or "").strip()
        park_name = (result.get("park") or "").strip()
        sources = [f"{art.get('title', '')[:120]} ({art.get('url', '')})"]

        targets: list[dict] = []

        if park_name:
            park = find_park(mcur, park_name)
            if park:
                for row, dist in nearby_complexes(mcur, park["lat"], park["lon"], PARK_RADIUS_M):
                    scaled = rating * (1 - 0.4 * (dist / PARK_RADIUS_M))
                    targets.append({
                        "name": row["name"],
                        "district": row["district"] or district,
                        "lat": row["lat"],
                        "lon": row["lon"],
                        "rating": round(scaled, 1),
                        "reason": f"рядом парк «{park['name']}» ({reason})",
                        "sources": sources,
                        "sentiment": sentiment,
                    })

        if lrt_station:
            station = find_lrt_station(mcur, lrt_station)
            if station:
                for row, dist in nearby_complexes(mcur, station["lat"], station["lon"], LRT_RADIUS_M):
                    scaled = rating * (1 - 0.4 * (dist / LRT_RADIUS_M))
                    targets.append({
                        "name": row["name"],
                        "district": row["district"] or district,
                        "lat": row["lat"],
                        "lon": row["lon"],
                        "rating": round(scaled, 1),
                        "reason": reason,
                        "sources": sources,
                        "sentiment": sentiment,
                    })

        if not targets and name:
            match = find_complex_match(mcur, name)
            if match:
                targets.append({
                    "name": match["name"],
                    "district": match["district"] or district,
                    "lat": match["lat"],
                    "lon": match["lon"],
                    "rating": rating,
                    "reason": reason,
                    "sources": sources,
                    "sentiment": sentiment,
                })

        if not targets:
            n_skipped_no_geo += 1
            print(f"[SKIP-GEO] нет геопривязки для '{name or lrt_station or park_name}' -- {art['title'][:90]}", flush=True)
            time.sleep(1)
            continue

        n_geocoded += 1
        for t in targets:
            if location_upsert.upsert_location(hcur, t) is not None:
                n_upserts += 1
        hconn.commit()
        time.sleep(1)

    mconn.close()
    hconn.close()
    print(
        f"готово: статей обработано={n_analyzed} (llm-ошибок={n_llm_failed}), "
        f"релевантных={n_relevant}, геопривязано={n_geocoded}, пропущено без гео={n_skipped_no_geo}, "
        f"апсертов локаций={n_upserts}",
        flush=True)


if __name__ == "__main__":
    main()
