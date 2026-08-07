#!/usr/bin/env python3
"""Ежедневный сбор новостей о недвижимости Астаны в таблицу news (krisha_bot).
Источник: Google News RSS (по доменам СМИ) + og:image с страниц статей.
Молчит при успехе (для no_agent крона)."""
import re, sys, json, html, time, urllib.request, urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras

BASE = Path("/home/nik/krisha_bot")


def load_database_url() -> str:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return "postgresql://krisha@localhost/krisha_bot"


def conn():
    return psycopg2.connect(load_database_url().rsplit("/", 1)[0] + "/krisha_bot")

# 4-уровневый список запросов (см. задачу "тепловая карта хайпа" — раньше
# было 4 общих фразы, поэтому и точек на карте было мало: 80% обсуждений
# идёт по именам ЖК, а не общими словами). Порядок = порядок в
# "Итог-приоритет" из спеки: сначала поимённые (дают больше всего точек).

# Уровень 3 — топ-50 ЖК Астаны для поимённого мониторинга (см. Notion/задачу).
# Список статичный (проверенные написания), ДОПОЛНЯЕТСЯ динамически топ-30
# по listings_count из complexes в load_queries() — так ловим и известные
# бренды, и то, что реально набирает объявления прямо сейчас, даже если в
# этот статичный список ещё не попало.
COMPLEX_NAMES = [
    # BI Group
    "Parkside Astana", "Vivaldi ЖК Астана", "La Vie ЖК Астана", "Akbulak Riviera",
    "Garden View ЖК Астана", "Aisar ЖК Астана", "AruPark ЖК", "GreenLine ЖК Астана",
    "Capital Park ЖК Астана", "Auez ЖК Астана", "MOD Urban ЖК", "Nexpo Aura ЖК",
    "Jetisu ЖК Астана", "Expo Plaza ЖК", "UIA.BIRLIK ЖК", "Park City Forum ЖК",
    "Koktobe City ЖК",
    # Элит/бизнес
    "The One Bazis ЖК", "Dara Residence ЖК", "Европа Сити ЖК Астана",
    "Swiss Collection ЖК", "GRAND MONACO ЖК Астана", "Highvill Astana",
    "Highvill Ishim", "London ЖК Астана", "England ЖК Астана", "Вивальди ЖК",
    "LANDMARK GOLD ЖК", "SALZBURG ЖК Астана", "LEVEL ЖК Астана",
    # Масс-маркет/комфорт
    "Imran ЖК Астана", "Salman City ЖК", "Altyn Säulet ЖК", "Rauda ЖК Астана",
    "Tasty ЖК Астана", "Фирдаус ЖК Астана", "Aviator 2 ЖК", "Altyn City ЖК",
    "Time City ЖК Астана", "Alatau Park ЖК", "PARKLAND ЖК Астана", "Manar ЖК Астана",
    "W TOWERS ЖК", "UIA.TARIH ЖК", "Sharyn ЖК Астана", "Turan Palace ЖК",
    "Galaxy Star ЖК", "Мирадж ЖК Астана", "Столичный 2 ЖК", "Evolution ЖК Астана",
]

_QUERIES_L1_MONEY = [
    "ипотека Казахстан 2026", "Отбасы банк ипотека", "ипотека 2% Астана",
    "НДС застройщики 2026", "цены на квартиры Астана", "квадратный метр Астана цена",
    "льготная ипотека Казахстан", "арендное жильё очередь",
]
_QUERIES_L2_DISTRICTS = [
    "левый берег новостройки Астана", "район Есиль ЖК", "район Нура квартиры",
    "Ботанический сад Астана ЖК", "Триатлон парк ЖК", "проспект Туран новостройки",
    "Кабанбай батыра ЖК Астана", "район вокзала Нурлы Жол недвижимость",
]
_QUERIES_L4_HYPE = [
    # позитив
    "старт продаж ЖК Астана", "очередь с ночи ЖК Астана", "раскупили новостройку Астана",
    "котлован цена ЖК Астана", "сдача ЖК Астана 2026", "ключи выдача ЖК Астана",
    # негатив
    "долгострой Астана", "задержка сдачи ЖК Астана", "обманутые дольщики Астана",
    "ЖК не стоит покупать Астана", "проблемный застройщик Астана",
    # инвест
    "ЖК для инвестиций Астана", "перепродажа новостройки Астана", "флиппинг квартиры Астана",
]

QUERIES = [
    "Астана недвижимость",
    "Астана новостройки ЖК",
    "ипотека Казахстан жильё",
    "рынок недвижимости Астана",
    *_QUERIES_L1_MONEY,
    *_QUERIES_L2_DISTRICTS,
    *COMPLEX_NAMES,
    *_QUERIES_L4_HYPE,
]


def load_queries() -> list[str]:
    """QUERIES + топ-30 ЖК по listings_count из complexes (динамически, не
    только статичный список выше) — самообновляется по мере роста базы,
    ловит то, что реально набирает объявления прямо сейчас."""
    out = list(QUERIES)
    try:
        db = conn()
        cur = db.cursor()
        cur.execute("""
            SELECT DISTINCT ON (lower(trim(complex_name))) complex_name, COUNT(*) OVER (PARTITION BY lower(trim(complex_name))) AS cnt
            FROM apartment_listings
            WHERE complex_name IS NOT NULL AND complex_name != ''
            ORDER BY lower(trim(complex_name)), cnt DESC
        """)
        rows = cur.fetchall()
        db.close()
        top = sorted(rows, key=lambda r: r[1] or 0, reverse=True)[:30]
        seen_lower = {q.lower() for q in out}
        for name, _cnt in top:
            q = f"{name} ЖК Астана"
            if q.lower() not in seen_lower:
                out.append(q)
                seen_lower.add(q.lower())
    except Exception as e:
        print(f"# load_queries: top-complexes fallback failed: {e}", file=sys.stderr)
    return out
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
MAX_IMAGE_FETCH = 10  # сколько страниц статей открываем за прогон ради фото

# ── Krisha.kz как источник (не только Google News RSS) ─────────────────────
# У Крыши свой полноценный редакционный раздел: /content/news (новости
# рынка) и /content/articles (аналитика/гайды) — см. задачу "тепловая карта
# хайпа", п.3 "Krysha.kz — да, новости есть и ведутся". Обе страницы —
# серверный рендер (не SPA), заголовок/анонс/картинка уже есть в HTML
# листинга — можно вытащить одним запросом на раздел, без захода на каждую
# статью отдельно (в отличие от Google News, где нужен отдельный og:image
# запрос на каждую ссылку).
KRISHA_CONTENT_PATHS = ["/content/news", "/content/articles"]
_KRISHA_ARTICLE_RE = re.compile(
    r'href="(/content/(?:news|articles)/\d{4}/[a-z0-9\-]+)"[^>]*>.*?'
    r'src="([^"]+)"[^>]*alt="[^"]*"[^>]*>.*?'
    r'<h4 class="article-row__title"[^>]*>(.*?)</h4>'
    r'(?:<div class="article-row__desc"[^>]*>(.*?)</div>)?',
    re.S)


def fetch_krisha_content(path: str) -> list[dict]:
    """Список [{title,url,source,image,summary}] с одной страницы раздела
    Крыши (/content/news или /content/articles). Заголовок/анонс/картинка
    уже в HTML листинга — доп. запросов на сами статьи не нужно."""
    url = "https://krisha.kz" + path
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read().decode("utf-8", errors="replace")
    out, seen_local = [], set()
    for m in _KRISHA_ARTICLE_RE.finditer(body):
        rel_url, img, title, desc = m.groups()
        if rel_url in seen_local:
            continue
        seen_local.add(rel_url)
        out.append({
            "title": html.unescape(strip_tags(title)),
            "url": "https://krisha.kz" + rel_url,
            "source": "Krisha.kz",
            "image": img,
            "summary": html.unescape(strip_tags(desc)) if desc else None,
        })
    return out


def get_rss(q: str) -> str:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "ru", "gl": "KZ", "ceid": "KZ:ru"})
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", errors="replace")


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()


# классифайды/доски объявлений — НЕ СМИ, в новости не включаем
SOURCE_BLOCK = {
    "krisha.kz", "korter.kz", "homsters.kz", "noz.kz", "etagi.com",
    "olx.kz", "slando.kz", "market.kz", "satu.kz", "avito.kz", "2gis.kz",
}
# явные маркеры объявлений в заголовке
AD_TITLE = re.compile(r"без комиссии|горячая цена|срочно продам|срочно сдам|продам квартиру|сдам квартиру|торг уместен", re.I)

# только новости, относящиеся к Астане (страна/другие города — мимо)
ASTANA_MARKERS = ("астан", "столиц", "нур-султан", "лрт", "талдыколь")


def is_astana_relevant(title: str) -> bool:
    t = (title or "").lower()
    return any(m in t for m in ASTANA_MARKERS)


def parse_rss(xml: str) -> list[dict]:
    out = []
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
        tm = re.search(r"<title>(.*?)</title>", it, re.S)
        lm = re.search(r"<link>(.*?)</link>", it, re.S)
        sm = re.search(r"<source url=\"[^\"]*\">(.*?)</source>", it, re.S)
        if not tm or not lm:
            continue
        title = strip_tags(tm.group(1))
        if not title or len(title) < 20:
            continue
        # отсечь классифайды и объявления
        if sm and (sm.group(1) or "").strip().lower() in SOURCE_BLOCK:
            continue
        if AD_TITLE.search(title):
            continue
        # только Астана: страна/другие города не берём
        if not is_astana_relevant(title):
            continue
        # картинка из RSS (media:content / enclosure)
        img = None
        mc = re.search(r'<media:content[^>]*url="([^"]+)"', it) or \
             re.search(r'<enclosure[^>]*url="([^"]+)"', it)
        if mc:
            img = html.unescape(mc.group(1))
        out.append({
            "title": html.unescape(title),
            "url": html.unescape(lm.group(1).strip()),
            "source": strip_tags(sm.group(1)) if sm else "",
            "image": img,
        })
    return out


def get_og_image(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=12) as r:
            body = r.read(400_000).decode("utf-8", errors="replace")
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', body) or \
            re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', body)
        return html.unescape(m.group(1)) if m else None
    except Exception:
        return None


def extract_summary(content: str) -> str:
    """Первые 1-2 абзаца статьи из HTML."""
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return ""
    soup = BeautifulSoup(content, "html.parser")
    seen = []
    for p in soup.select("article p, .article-content p, .content p, .news-content p, p"):
        t = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
        if len(t) > 80:
            seen.append(t)
        if len(seen) >= 2:
            break
    text = " ".join(seen).strip()
    return text[:600] if text else ""


def enrich_images_playwright(items: list[dict], limit: int = 12) -> None:
    """Достаём og:image и выжимку текста через Playwright (Google News разрезолв)."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"# playwright недоступен: {e}", file=sys.stderr)
        return
    done = 0
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(user_agent=UA)
        for it in items:
            if it.get("image") and it.get("summary"):
                continue
            if done >= limit and it.get("image"):
                continue
            try:
                pg.goto(it["url"], wait_until="domcontentloaded", timeout=25000)
                pg.wait_for_timeout(2500)
                content = pg.content()
                if not it.get("image"):
                    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', content) or \
                        re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', content)
                    if m:
                        it["image"] = html.unescape(m.group(1))
                        done += 1
                if not it.get("summary"):
                    it["summary"] = extract_summary(content)
            except Exception:
                pass
        b.close()


def main() -> None:
    db = conn()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT url FROM news WHERE ts > now() - interval '30 days'")
    seen_urls = {r["url"] for r in cur.fetchall()}

    items: list[dict] = []
    seen_this_run: set[str] = set()

    # Krisha.kz editorial-раздел — первым, до общего RSS-обхода: это
    # проверенно релевантный источник (не поиск по ключевым словам), и он
    # должен пережить обрезку items[:100] ниже (2 запроса на весь список).
    for path in KRISHA_CONTENT_PATHS:
        try:
            for it in fetch_krisha_content(path):
                if it["url"] in seen_urls or it["url"] in seen_this_run:
                    continue
                items.append(it)
                seen_this_run.add(it["url"])
            time.sleep(2)
        except Exception as e:
            print(f"# krisha content error {path}: {e}", file=sys.stderr)

    for q in load_queries():
        try:
            for it in parse_rss(get_rss(q)):
                if it["url"] in seen_urls or it["url"] in seen_this_run:
                    continue
                items.append(it)
                seen_this_run.add(it["url"])
            time.sleep(2)
        except Exception as e:
            print(f"# rss error {q}: {e}", file=sys.stderr)

    # Лимит подняли с 15 до 100 — со старыми 4 общими запросами 15/день
    # хватало с запасом, но с поимёнными запросами по ~50-80 ЖК (см.
    # load_queries()) 15 сразу же душило самую ценную часть охвата.
    items = items[:100]
    # сначала простой og:image (для прямых ссылок), потом Playwright для Google News
    for it in items:
        if not it["image"] and not it["url"].startswith("https://news.google.com"):
            it["image"] = get_og_image(it["url"])
            time.sleep(0.8)
    enrich_images_playwright(items, limit=12)
    for it in items:
        try:
            cur.execute(
                "INSERT INTO news (title, source, url, image_url, summary) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (url) DO NOTHING",
                (it["title"][:500], it["source"][:100], it["url"], it["image"], (it.get("summary") or None)))
        except Exception as e:
            print(f"# insert error: {e}", file=sys.stderr)
    # дозаполняем выжимки у свежих новостей без summary (через Playwright)
    cur.execute(
        "SELECT id, url, image_url FROM news WHERE summary IS NULL AND ts > now() - interval '3 days' LIMIT 12")
    backfill = [{"id": r["id"], "url": r["url"], "image": r["image_url"], "summary": None}
                for r in cur.fetchall()]
    if backfill:
        enrich_images_playwright(backfill, limit=12)
        for b in backfill:
            if b.get("summary"):
                cur.execute("UPDATE news SET summary = %s WHERE id = %s",
                            (b["summary"][:600], b["id"]))
    # уборка старья
    try:
        cur.execute("DELETE FROM news WHERE ts < now() - interval '14 days'")
    except Exception:
        pass
    db.commit()
    db.close()
    # тихо: stdout пустой


if __name__ == "__main__":
    main()
