"""
Дополнительные маршруты веб-терминала:
  /admin/settings           — ползунки главных настроек скоринга + монетизация
  /admin/settings/save      — сохранение (POST, JSON)
  /admin/monetization/toggle— вкл/выкл монетизацию одной кнопкой
  /admin/project/start|stop — запуск/остановка парсеров (systemctl через sudo)
  /admin/project/status     — статус сервисов (JSON)
  /admin/top10              — 10 лучших объектов по скору

Подключение в bot/admin_web.py (внутри create_admin_app, после создания app):

    from terminal_extras import make_extras_router
    app.include_router(make_extras_router(templates))

Требуется sudoers-правило (см. INSTALL_EXTRAS.md), чтобы кнопка
"Запустить проект" могла дёргать systemctl без пароля.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re

import httpx
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from bot.db import settings as app_settings
from bot.db.pg import fetch

logger = logging.getLogger(__name__)

# Сервисы, которыми управляет кнопка "Запустить проект".
# Веб-терминал (krisha-web) сюда не входит — он всегда работает.
PROJECT_SERVICES = ["krisha-rental", "krisha-apartments", "krisha-alerts", "krisha-korter", "krisha-homsters", "krisha-market"]

# Настройки, редактируемые ползунками: key -> (подпись, min, max, шаг, единица)
# (label, min, max, step, unit, ГРУППА)
SLIDER_SETTINGS = {
    "DEPOSIT_RATE":      ("Ставка депозита (KZT)", 5, 25, 0.5, "%", "💰 Финансовые допущения"),
    "APPRECIATION_PCT":  ("Ожидаемый рост цены кв.м", 0, 20, 0.5, "%/год", "💰 Финансовые допущения"),
    "MORTGAGE_RATE":     ("Ставка ипотеки", 5, 25, 0.5, "%", "💰 Финансовые допущения"),
    "MORTGAGE_YEARS":    ("Срок ипотеки", 5, 30, 1, "лет", "💰 Финансовые допущения"),
    "MORTGAGE_DOWN_PCT": ("Первоначальный взнос", 10, 50, 5, "%", "💰 Финансовые допущения"),
    "REALTOR_FEE_PCT":   ("Комиссия риелтора", 0, 5, 0.5, "%", "💰 Финансовые допущения"),
    "ALERT_THRESHOLD":   ("Порог скора для алертов", 50, 90, 1, "баллов", "🎯 Скоринг"),
    "POPUP_WIDTH_PX":    ("Ширина превью объявления на карте", 280, 600, 10, "px", "👁 Настройки вида"),
    "HEX_EDGE_M":        ("Гексагон-сетка: ребро (м)", 30, 200, 10, "м", "⬡ Гексагоны"),
    "SCORE_W_PRICE":     ("Вес: цена vs локальный рынок", 0, 100, 5, "%", "⚖️ Веса Deal Score"),
    "SCORE_W_LOCATION":  ("Вес: локация + инфраструктура", 0, 100, 5, "%", "⚖️ Веса Deal Score"),
    "SCORE_W_QUALITY":   ("Вес: качество ЖК (класс/год/рейтинг)", 0, 100, 5, "%", "⚖️ Веса Deal Score"),
    "SCORE_W_MARKET":    ("Вес: доходность аренды + ликвидность", 0, 100, 5, "%", "⚖️ Веса Deal Score"),
    "SCORE_W_RISK":      ("Вес: риск (этаж/риелтор)", 0, 100, 5, "%", "⚖️ Веса Deal Score"),
    "VIEWCOUNT_BATCH":   ("Просмотров за цикл (Playwright, ~раз в час)", 0, 100, 5, "шт.", "👁 Просмотры (Playwright)"),
    "VIEWCOUNT_DELAY_MIN": ("Задержка между запросами — мин", 3, 30, 1, "с", "👁 Просмотры (Playwright)"),
    "VIEWCOUNT_DELAY_MAX": ("Задержка между запросами — макс", 5, 45, 1, "с", "👁 Просмотры (Playwright)"),
    "VIEWCOUNT_MIN_AGE_HOURS": ("Не повторять чаще, чем раз в N часов", 1, 72, 1, "ч", "👁 Просмотры (Playwright)"),
    "PARSER_MAX_PAGES":  ("Страниц Krisha за цикл (свежие)", 1, 40, 1, "стр.", "🕷 Обход парсера"),
    "RENTAL_MAX_PAGES":  ("Страниц Крыши за цикл аренды (было 10 — покрывало ~5% рынка)", 5, 100, 5, "стр.", "🕷 Обход парсера"),
    "DEEP_SWEEP_BATCH":  ("Глубокий обход: доп. страниц за цикл (0=выкл)", 0, 20, 1, "стр.", "🕷 Обход парсера"),
    "DETAIL_FETCH_BATCH": ("Деталей/координат за цикл (полскора+полслучайно)", 2, 40, 1, "шт.", "🕷 Обход парсера"),
    "COORD_BACKFILL_BATCH": ("Добивка координат по ВСЕЙ базе за цикл (~3-5 стр. объявлений/час)", 0, 200, 5, "шт.", "🕷 Обход парсера"),
    "ARCHIVE_CHECK_BATCH": ("Проверка архивности за цикл (2.5-5с/шт — влияет на длину цикла)", 15, 400, 5, "шт.", "🕷 Обход парсера"),
    "RENTAL_ARCHIVE_CHECK_BATCH": ("Проверка архивности аренды за цикл (2.5-5с/шт)", 0, 200, 5, "шт.", "🕷 Обход парсера"),
    "COORD_FETCH_DELAY_MIN": ("Задержка между запросами деталей — мин", 3, 30, 1, "с", "🕷 Обход парсера"),
    "COORD_FETCH_DELAY_MAX": ("Задержка между запросами деталей — макс", 5, 45, 1, "с", "🕷 Обход парсера"),
}

# Пояснения к отдельным ползункам (только там, где смысл не очевиден из
# названия) — показываются под ползунком на /admin/settings.
SLIDER_DESCRIPTIONS = {
    "DEEP_SWEEP_BATCH": (
        "Постепенный обход всей выдачи Крыши вглубь (не только свежие "
        "объявления) — парсер запоминает позицию между циклами и с каждым "
        "циклом читает ещё DEEP_SWEEP_BATCH страниц дальше, постепенно "
        "закрывая весь бэклог, а не только свежие страницы."
    ),
    "DETAIL_FETCH_BATCH": (
        "Из объявлений, спарсенных за этот цикл, столько получают дорогой "
        "запрос детальной страницы (координаты/фото/этаж и т.п.): половина — "
        "топ по предварительному скору, половина — случайные, чтобы на карте "
        "были видны не только «хорошие» объявления."
    ),
    "COORD_BACKFILL_BATCH": (
        "Отдельно от обычного обхода — досасывает координаты/ЖК/фото по ВСЕЙ "
        "базе (для любых объявлений, где их до сих пор нет), в порядке "
        "старых→новых, независимо от того, когда объявление было спаршено. "
        "Сейчас может быть принудительно выставлено в 0, если идёт "
        "трёхдневный массовый backfill-скрипт — по завершении он сам вернёт "
        "это значение обратно."
    ),
}


async def hex_price_cells(lat0: float, lon0: float, rooms: int | None = None) -> tuple[list[dict], list[dict]]:
    """Цена/м² по гексагону (edge 300м) вокруг точки + 6 соседей — отдельно
    продажа и аренда. Общая логика для страницы ЖК и мини-карты в попапе
    объявления на дашборде.

    rooms: если задан — аренда считается ТОЛЬКО по объявлениям с такой же
    комнатностью, и дополнительно считается avg_price (средняя итоговая
    аренда в месяц, не ₸/м²) — ₸/м² для аренды путают с итоговой ценой
    (видно как "5 тыс." и кажется багом, хотя это ₸/м²/мес)."""
    from bot.core.hexgrid import (
        hex_id as _hex_id, neighbors as _hex_neighbors, hex_corners as _hex_corners,
    )
    HEX_EDGE = 300.0
    dlat, dlon = 0.012, 0.019  # ~1.3км в каждую сторону — с запасом на 2 кольца гекса

    def _build(rows, with_total=False, archive_rows=None) -> list[dict]:
        buckets: dict[str, list[float]] = {}
        totals: dict[str, list[float]] = {}
        for r in rows:
            hid = _hex_id(float(r["lat"]), float(r["lon"]), HEX_EDGE)
            buckets.setdefault(hid, []).append(float(r["price"]) / float(r["area"]))
            if with_total:
                totals.setdefault(hid, []).append(float(r["price"]))
        # Гексагоны без АКТИВНЫХ объявлений — не обязательно "нет данных":
        # если там что-то продалось (ушло в архив), последняя цена перед
        # архивацией всё ещё полезный ориентир вместо пустой серой клетки.
        archive_buckets: dict[str, list[float]] = {}
        archive_totals: dict[str, list[float]] = {}
        if archive_rows:
            for r in archive_rows:
                hid = _hex_id(float(r["lat"]), float(r["lon"]), HEX_EDGE)
                archive_buckets.setdefault(hid, []).append(float(r["price"]) / float(r["area"]))
                if with_total:
                    archive_totals.setdefault(hid, []).append(float(r["price"]))
        self_hid = _hex_id(lat0, lon0, HEX_EDGE)
        wanted = [("здесь", self_hid)] + [
            (f"сосед {i+1}", h) for i, h in enumerate(_hex_neighbors(self_hid))
        ]
        cells = []
        for label, hid in wanted:
            vals = buckets.get(hid, [])
            tvals = totals.get(hid, [])
            is_archived = False
            if not vals and archive_buckets.get(hid):
                vals = archive_buckets[hid]
                tvals = archive_totals.get(hid, [])
                is_archived = True
            cells.append({
                "label": label,
                "avg_m2": round(sum(vals) / len(vals)) if vals else None,
                "avg_price": round(sum(tvals) / len(tvals)) if tvals else None,
                "count": len(vals),
                "is_self": label == "здесь",
                "is_archived": is_archived,
                "corners": [list(c) for c in _hex_corners(hid, HEX_EDGE)],
            })
        return cells

    nearby_sale = await fetch("""
        SELECT lat, lon, price, area FROM apartment_listings
        WHERE lat BETWEEN $1 AND $2 AND lon BETWEEN $3 AND $4
          AND is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
          AND price > 500000 AND area > 0
    """, lat0 - dlat, lat0 + dlat, lon0 - dlon, lon0 + dlon)
    nearby_sale_archived = await fetch("""
        SELECT lat, lon, price, area FROM apartment_listings
        WHERE lat BETWEEN $1 AND $2 AND lon BETWEEN $3 AND $4
          AND is_active = FALSE AND archived_at IS NOT NULL
          AND COALESCE(is_duplicate, FALSE) = FALSE
          AND price > 500000 AND area > 0
          AND archived_at > now() - interval '30 days'
    """, lat0 - dlat, lat0 + dlat, lon0 - dlon, lon0 + dlon)
    rental_params = [lat0 - dlat, lat0 + dlat, lon0 - dlon, lon0 + dlon]
    rental_room_cond = ""
    if rooms:
        rental_room_cond = "AND rooms = $5"
        rental_params.append(rooms)
    nearby_rental = await fetch(f"""
        SELECT lat, lon, price, area FROM rental_listings
        WHERE lat BETWEEN $1 AND $2 AND lon BETWEEN $3 AND $4
          AND price > 0 AND area > 0
          AND is_active IS NOT FALSE AND last_seen > now() - interval '30 days'
          {rental_room_cond}
    """, *rental_params)
    nearby_rental_archived = await fetch(f"""
        SELECT lat, lon, price, area FROM rental_listings
        WHERE lat BETWEEN $1 AND $2 AND lon BETWEEN $3 AND $4
          AND price > 0 AND area > 0
          AND is_active = FALSE AND archived_at > now() - interval '30 days'
          {rental_room_cond}
    """, *rental_params)
    return (_build(nearby_sale, archive_rows=nearby_sale_archived),
            _build(nearby_rental, with_total=True, archive_rows=nearby_rental_archived))


_UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "static", "uploads")
_MAX_UPLOAD_SIDE = 1600  # px — большие фото ужимаем, чтобы не раздувать диск/трафик


async def _save_uploaded_photos(files: list, kind: str, entity_id: int) -> list[str]:
    """Сохраняет загруженные админом файлы на диск (static/uploads/{kind}/{id}/)
    и возвращает список публичных /static-путей. Невалидные (не картинки) файлы
    пропускаются молча. `kind` — "complexes" или "developers"."""
    import io
    import time

    from PIL import Image

    out_dir = os.path.join(_UPLOAD_DIR, kind, str(entity_id))
    os.makedirs(out_dir, exist_ok=True)
    urls: list[str] = []
    for i, f in enumerate(files[:3]):
        raw = await f.read()
        if not raw:
            continue
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            if max(img.size) > _MAX_UPLOAD_SIDE:
                img.thumbnail((_MAX_UPLOAD_SIDE, _MAX_UPLOAD_SIDE))
        except Exception:
            continue  # не картинка / битый файл — пропускаем
        fname = f"{int(time.time())}_{i}.jpg"
        img.save(os.path.join(out_dir, fname), "JPEG", quality=85)
        urls.append(f"/static/uploads/{kind}/{entity_id}/{fname}")
    return urls


_PHOTO_CACHE_DIR = os.path.join(os.path.dirname(__file__), "static", "cache", "photos")
os.makedirs(_PHOTO_CACHE_DIR, exist_ok=True)
# Только известные источники фото объявлений/ЖК — proxy не должен превращаться
# в открытый прокси для произвольных URL (SSRF).
_PHOTO_ALLOWED_HOSTS = (
    "kcdn.online", "krisha.kz",          # Крыша (фото объявлений)
    "homeportal.kz",                     # api.homeportal.kz — фото ЖК (реестр КЖК)
    "bazis.kz",                          # admin.sales.bazis.kz / admin.shablon.bazis.kz / bazis-online.kz
    "orda-invest.kz",                    # new.orda-invest.kz — фото новостроек ORDA
    "profitbase.ru",                     # pb4678.profitbase.ru — планировки
    "bi.group",                          # s3.bi.group — фото BI Group
    "sensata.kz",                        # фото Sensata Group
    "homsters.kz",                       # getImage?imageId= — фото Homsters
    "svoydom.kz",                        # фото планировок Svoy Dom (новостройки)
)
_PHOTO_EXT_WHITELIST = (".jpg", ".jpeg", ".png", ".webp")


def _photo_cache_path(u: str) -> str | None:
    """Тот же путь на диске, что использует /img-proxy для этого URL —
    вынесено отдельно, чтобы прогрев кэша (см. prewarm_photo_cache) и сам
    прокси не расходились в логике именования файла."""
    from urllib.parse import urlparse
    parsed = urlparse(u)
    host = (parsed.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in _PHOTO_ALLOWED_HOSTS):
        return None
    ext = os.path.splitext(parsed.path)[1].lower()
    if ext not in _PHOTO_EXT_WHITELIST:
        ext = ".jpg"
    fname = hashlib.sha256(u.encode()).hexdigest() + ext
    return os.path.join(_PHOTO_CACHE_DIR, fname)


async def prewarm_photo_cache(u: str) -> bool:
    """Скачивает и кэширует фото заранее (не по факту первого открытия
    попапа пользователем, а сразу после парсинга) — раньше первая загрузка
    любого ранее непросмотренного фото занимала 2-3с (синхронный fetch на
    kcdn.online прямо во время открытия попапа). Возвращает True если файл
    уже в кэше или успешно закэширован."""
    fpath = _photo_cache_path(u)
    if not fpath:
        return False
    if os.path.exists(fpath):
        return True
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as c:
            resp = await c.get(u, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            tmp_path = fpath + ".tmp"
            with open(tmp_path, "wb") as f:
                f.write(resp.content)
            os.replace(tmp_path, fpath)
        return True
    except Exception:
        return False



def _load_db_url(db_name: str) -> str:
    from pathlib import Path
    env = Path("/home/nik/krisha_bot/.env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().rsplit("/", 1)[0] + "/" + db_name
    return f"postgresql://krisha@localhost/{db_name}"


def _hype_db_conn():
    import psycopg2
    return psycopg2.connect(_load_db_url("hype_tracker"))


def make_extras_router(templates) -> APIRouter:
    router = APIRouter()

    def is_authed(request: Request) -> bool:
        return request.cookies.get("admin_auth") == "1"

    # Доступно во всех шаблонах: {{ is_admin(request) }} — для скрытия
    # админ-элементов на публичных страницах
    templates.env.globals["is_admin"] = is_authed

    # 3 уровня доступа (задача "надо разделить доступ к сайту на 3 уровня",
    # 2026-08-07, переформулировано 2026-08-12): admin — admin_auth cookie
    # (как раньше, без изменений). subscriber — залогинен через Telegram И
    # вручную выдан full_access администратором (/admin/site-users) — все
    # объявления открыты, недоступна только админка. public — аноним ИЛИ
    # залогинен через Telegram, но full_access ещё не выдан (регистрация
    # сама по себе доступ больше не открывает): на карте — только
    # новостройки (market_type='primary') СО ВСЕМИ фильтрами + тепловые
    # карты (те отдельные роуты, ничем не гейтятся); аренда/вторичка на
    # карте не показываются вовсе; в попапе объявления скрыт блок "похожие
    # рядом"; страницы ЖК (/admin/complex/{id}) — заглушка с призывом
    # получить доступ.

    _full_access_col_ready = False

    async def get_user_tier(request: Request) -> str:
        nonlocal _full_access_col_ready
        if is_authed(request):
            return "admin"
        session = request.cookies.get("site_session")
        if session:
            from bot.core.site_auth import get_user_by_session, _ensure_full_access_column
            if not _full_access_col_ready:
                await _ensure_full_access_column()
                _full_access_col_ready = True
            user = await get_user_by_session(session)
            if user and user.get("full_access"):
                return "subscriber"
        return "public"
    @router.get("/admin/analytics/hype", response_class=HTMLResponse)
    async def hype_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("hype_analytics.html", {"request": request})

    @router.get("/admin/api/hype-hexes")
    async def hype_hexes(request: Request):
        from bot.db.pg import fetch as pg_fetch
        from bot.core.hexgrid import hex_id, hex_center
        rows = await pg_fetch("""
            SELECT name, lat, lon, listings_count, rental_listings_count, sold_count, avg_price_m2
            FROM complexes
            WHERE lat IS NOT NULL AND lon IS NOT NULL
              AND COALESCE(listings_count,0) + COALESCE(rental_listings_count,0) + COALESCE(sold_count,0) > 0
        """)
        import re as _re
        junk = _re.compile(r"(?i)^(жк|жк |апартаменты|квартиры|дом|улица|район|жилой комплекс|жил|новостройк)[\s\-\W]*$|^[^а-яёa-z]+$|^[\w\s]{1,2}$")
        recs = [dict(r) for r in rows if not junk.search(str(r["name"] or ""))]
        if not recs:
            return JSONResponse({"hexes": []})
        n = len(recs)
        def rank_norm(vals):
            order = sorted(range(n), key=lambda i: vals[i])
            out = [0.0] * n
            for k, i in enumerate(order):
                out[i] = k / (n - 1) if n > 1 else 0.5
            return out
        volume = [1.5 * (r["sold_count"] or 0) + (r["listings_count"] or 0) + 0.5 * (r["rental_listings_count"] or 0) for r in recs]
        prices = [r["avg_price_m2"] or 0 for r in recs]
        rv = rank_norm(volume)
        rp = rank_norm(prices)
        scores = [0.65 * rv[i] + 0.35 * rp[i] for i in range(n)]
        rs = rank_norm(scores)

        # ЖК рисовались гексагонами ПРЯМО вокруг своих координат (не привязка
        # к общей сетке) — у соседних домов гексы визуально перекрывались.
        # Правильный гекс-слой (как у цены/шума) — снэпим каждый ЖК к общей
        # сетке (тот же hex_id/hex_center, что и остальные гекс-слои проекта)
        # и агрегируем несколько ЖК в одной ячейке взвешенным средним по объёму.
        EDGE_M = 100.0
        cells: dict[str, dict] = {}
        for i in range(n):
            hid = hex_id(float(recs[i]["lat"]), float(recs[i]["lon"]), EDGE_M)
            cell = cells.setdefault(hid, {"names": [], "wsum": 0.0, "vsum": 0.0})
            w = max(volume[i], 0.1)
            cell["wsum"] += (0.24 + 0.75 * rs[i]) * w
            cell["vsum"] += w
            cell["names"].append(recs[i]["name"])
        hexes = []
        for hid, cell in cells.items():
            clat, clon = hex_center(hid, EDGE_M)
            label = cell["names"][0] + (f" +{len(cell['names'])-1}" if len(cell["names"]) > 1 else "")
            hexes.append({"name": label, "lat": clat, "lon": clon,
                          "score": round(cell["wsum"] / cell["vsum"], 4) if cell["vsum"] else 0.24})
        hexes.sort(key=lambda h: h["score"], reverse=True)
        return JSONResponse({"hexes": hexes})

    @router.get("/admin/api/hype-tracker")
    async def hype_tracker_info(request: Request):
        db = _hype_db_conn()
        cur = db.cursor()
        cur.execute("""
            SELECT h.id, h.name, h.url, h.rtype,
                   (SELECT COUNT(*) FROM hype_resource_runs r WHERE r.resource_id = h.id) AS runs,
                   (SELECT COALESCE(SUM(items_found),0) FROM hype_resource_runs r WHERE r.resource_id = h.id) AS total_items
            FROM hype_resources h ORDER BY h.name""")
        cols = [d[0] for d in cur.description]
        resources = [dict(zip(cols, row)) for row in cur.fetchall()]
        cur.execute("""
            SELECT s.id, s.ts, s.period,
                   (SELECT COUNT(*) FROM hype_resource_runs r WHERE r.snapshot_id = s.id) AS resources_used,
                   (SELECT COALESCE(SUM(items_found),0) FROM hype_resource_runs r WHERE r.snapshot_id = s.id) AS items_total
            FROM hype_snapshots s ORDER BY s.ts DESC LIMIT 60""")
        cols2 = [d[0] for d in cur.description]
        snapshots = [dict(zip(cols2, row)) for row in cur.fetchall()]
        for s in snapshots:
            if s.get("ts") is not None:
                s["ts"] = str(s["ts"])
        cur.execute("""
            SELECT COALESCE(run.created_at, s.ts) AS ts, r.name, run.items_found
            FROM hype_resource_runs run
            JOIN hype_resources r ON r.id = run.resource_id
            LEFT JOIN hype_snapshots s ON s.id = run.snapshot_id
            WHERE r.rtype = 'news' AND run.items_found > 0
            ORDER BY ts DESC LIMIT 400""")
        cols3 = [d[0] for d in cur.description]
        media_runs = [dict(zip(cols3, row)) for row in cur.fetchall()]
        for m in media_runs:
            if m.get("ts") is not None:
                m["ts"] = str(m["ts"])
        cur.close(); db.close()
        return JSONResponse({"resources": resources, "snapshots": snapshots, "media_runs": media_runs})

    @router.get("/admin/api/mortgage-banks")
    async def mortgage_banks_api(request: Request):
        """Публичный (карта на главной без логина) список банков/ипотечных
        программ — для подсказки 'в каких банках можно взять ипотеку' в
        превью объявления, большом попапе и на странице объявления
        (analytics_detail.html). Маленькая таблица (десятки строк) — отдаём
        целиком, подбор подходящих банков под конкретную цену/тип жилья
        считается на клиенте (см. matchMortgageBanks в dashboard.html)."""
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT b.slug, b.short_name, b.name, b.website, p.name AS program_name,
                   p.housing_type, p.rate_min, p.rate_max, p.down_payment_min_pct,
                   p.max_amount_tg, p.conditions, p.rate_note
            FROM mortgage_programs p JOIN banks b ON b.id = p.bank_id
            WHERE p.rate_min IS NOT NULL
            ORDER BY b.sort_order, p.rate_min
        """)
        return JSONResponse({"programs": [dict(r) for r in rows]})

    @router.get("/admin/banks", response_class=HTMLResponse)
    async def banks_page(request: Request):
        # Публичная страница (см. паттерн ЖК/застройщиков) — банки/ставки
        # полезны любому посетителю карты, не только админу.
        from bot.db.pg import fetch as pg_fetch
        banks = [dict(r) for r in await pg_fetch(
            "SELECT id, slug, name, short_name, description, website, phone, program_type, notes, sort_order, logo_url FROM banks ORDER BY sort_order, name")]
        progs = await pg_fetch("SELECT * FROM mortgage_programs ORDER BY id")
        by_bank: dict = {}
        for p in progs:
            by_bank.setdefault(p["bank_id"], []).append(p)
        for b in banks:
            b["programs"] = by_bank.get(b["id"], [])
        return templates.TemplateResponse("banks.html", {"request": request, "banks": banks})

    @router.get("/admin/banks/{slug}", response_class=HTMLResponse)
    async def bank_detail(request: Request, slug: str):
        from bot.db.pg import fetch, fetchrow
        bank = await fetchrow("SELECT * FROM banks WHERE slug = $1", slug)
        if not bank:
            return HTMLResponse("Банк не найден", status_code=404)
        programs = await fetch("SELECT * FROM mortgage_programs WHERE bank_id = $1 ORDER BY id", bank["id"])
        return templates.TemplateResponse("bank_detail.html", {"request": request, "bank": bank, "programs": programs})

    @router.get("/admin/mortgage-calculator", response_class=HTMLResponse)
    async def mortgage_calculator_page(request: Request):
        # Публичная страница (тот же паттерн, что /admin/banks) — расчёт
        # ежемесячного платежа по программам из /admin/api/mortgage-banks,
        # считается на клиенте (та же JSON-ручка, что и попап на карте).
        return templates.TemplateResponse("mortgage_calculator.html", {"request": request})

    @router.get("/admin/news", response_class=HTMLResponse)
    async def news_page(request: Request):
        from bot.db.pg import fetch as pg_fetch
        news = [dict(r) for r in await pg_fetch(
            "SELECT id, ts, title, source, url, image_url FROM news ORDER BY ts DESC LIMIT 30")]
        return templates.TemplateResponse("news.html", {"request": request, "news": news})

    @router.get("/admin/news/{nid}", response_class=HTMLResponse)
    async def news_detail(request: Request, nid: int):
        from bot.db.pg import fetchrow
        n = await fetchrow("SELECT * FROM news WHERE id = $1", nid)
        if not n:
            return HTMLResponse("Новость не найдена", status_code=404)
        return templates.TemplateResponse("news_detail.html", {"request": request, "n": dict(n)})

    @router.get("/admin/analytics/news-analysis", response_class=HTMLResponse)
    async def news_analysis_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("news_analysis.html", {"request": request})

    @router.get("/admin/api/news-analysis")
    async def news_analysis_api(request: Request, days: int = 90):
        days = days if days in (1, 3, 7, 30, 90) else 90
        db = _hype_db_conn()
        cur = db.cursor()
        cur.execute(
            "SELECT run_date, news_count, sources_count, threads_count, tokens_spent "
            "FROM news_analysis_runs WHERE run_date > CURRENT_DATE - %s::int "
            "ORDER BY run_date DESC LIMIT 90", (days,))
        cols = [d[0] for d in cur.description]
        runs = [dict(zip(cols, row)) for row in cur.fetchall()]
        for r in runs:
            if r.get("run_date") is not None:
                r["run_date"] = str(r["run_date"])
        cur.close(); db.close()
        return JSONResponse({"runs": runs})

    @router.get("/admin/analytics/transport", response_class=HTMLResponse)
    async def transport_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("transport_analytics.html", {"request": request})

    @router.get("/admin/api/hype-locations")
    async def hype_locations_api(request: Request, days: int | None = None):
        # БАГ (найден при расследовании "хайп только у ЛРТ"): раньше отдавали
        # ORDER BY rating DESC LIMIT 80 без учёта времени — ЛРТ-сид (169 из 212
        # локаций, рейтинг стабильно 45-85) вытеснял из топ-80 более свежие,
        # но ниже оценённые настоящие новостные хиты (rating 5-40). На деле
        # 67 из 80 отдаваемых записей были ЛРТ-сидом. LIMIT поднят до 300
        # (весь датасет — 212 строк, запас на рост), это отдаёт ВСЕ реальные
        # новостные хиты, а не только самые высокооценённые.
        # Параметр days — окно по hype_location_history.ts (задача "хайп по
        # периоду"): агрегируем по локации максимальный рейтинг за окно и
        # число упоминаний (строк истории) в этом окне — это и "как оно
        # выглядело тогда", и метрика "сколько раз упоминали".
        # Velocity/decay (см. п.5 спеки "тепловая карта хайпа" — формула
        # HypeScore): velocity = упоминаний за 24ч / (среднее/день за
        # предыдущие 7д + 1) — рост в 3-5х от базового уровня = "вспыхнуло".
        # decay — экспоненциальное затухание рейтинга по возрасту последнего
        # упоминания (τ=48ч, середина диапазона 36-72ч из спеки) — старый
        # хайп без новых упоминаний "остывает" на карте вместо вечного
        # горения. Оба считаются здесь и отдаются отдельными полями —
        # фронт красит по decayed_rating, а не по сырому rating.
        import math
        from datetime import datetime, timezone
        db = _hype_db_conn()
        cur = db.cursor()
        if days:
            cur.execute("""
                SELECT l.id, l.name, l.district, l.lat, l.lon,
                       MAX(h.rating) AS rating,
                       COUNT(h.id) AS mentions,
                       (array_agg(h.note ORDER BY h.ts DESC))[1] AS reason,
                       MAX(h.ts) AS last_seen,
                       AVG(h.sentiment) AS sentiment,
                       (SELECT COUNT(*) FROM hype_location_history h24
                        WHERE h24.location_id = l.id AND h24.ts >= now() - interval '24 hours') AS n_24h,
                       (SELECT COUNT(*) FROM hype_location_history h7
                        WHERE h7.location_id = l.id AND h7.ts >= now() - interval '7 days'
                          AND h7.ts < now() - interval '24 hours') / 6.0 AS avg_per_day_7d
                FROM hype_locations l
                JOIN hype_location_history h ON h.location_id = l.id
                WHERE l.lat IS NOT NULL AND l.lon IS NOT NULL
                  AND h.ts >= now() - (%s || ' days')::interval
                GROUP BY l.id, l.name, l.district, l.lat, l.lon
                HAVING MAX(h.rating) > 0
                ORDER BY rating DESC LIMIT 300""", (days,))
        else:
            cur.execute("""
                SELECT l.id, l.name, l.district, l.lat, l.lon, l.rating,
                       COALESCE((SELECT COUNT(*) FROM hype_location_history h
                                 WHERE h.location_id = l.id), 0) AS mentions,
                       l.reason, l.last_seen, l.sentiment,
                       (SELECT COUNT(*) FROM hype_location_history h24
                        WHERE h24.location_id = l.id AND h24.ts >= now() - interval '24 hours') AS n_24h,
                       (SELECT COUNT(*) FROM hype_location_history h7
                        WHERE h7.location_id = l.id AND h7.ts >= now() - interval '7 days'
                          AND h7.ts < now() - interval '24 hours') / 6.0 AS avg_per_day_7d
                FROM hype_locations l
                WHERE l.lat IS NOT NULL AND l.lon IS NOT NULL AND l.rating > 0
                ORDER BY l.rating DESC LIMIT 300""")
        cols = [d[0] for d in cur.description]
        locs = [dict(zip(cols, row)) for row in cur.fetchall()]
        DECAY_TAU_HOURS = 48.0
        now_ts = datetime.now(timezone.utc)
        for l in locs:
            last_seen = l.get("last_seen")
            if last_seen is not None:
                age_hours = max(0.0, (now_ts - last_seen).total_seconds() / 3600.0)
                l["decayed_rating"] = round((l.get("rating") or 0) * math.exp(-age_hours / DECAY_TAU_HOURS), 1)
                l["age_hours"] = round(age_hours, 1)
                l["last_seen"] = str(last_seen)
            else:
                l["decayed_rating"] = l.get("rating") or 0
                l["age_hours"] = None
            avg_7d = float(l.get("avg_per_day_7d") or 0)
            l["velocity"] = round((l.get("n_24h") or 0) / (avg_7d + 1), 2)
            l.pop("avg_per_day_7d", None)
        cur.close(); db.close()
        return JSONResponse({"locations": locs})

    @router.get("/admin/api/demolition-points")
    async def demolition_points_api(request: Request):
        """Точки домов под снос/реновацию (см. /admin/analytics/demolition и
        задачу "Снос кнопкой в тепловые карты") — для слоя на главной карте
        и карты на /admin/info#demolition. Публичный, как и сама карта —
        адреса из утверждённого перечня, не приватные данные."""
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT address, district, apartments, demolish_year, year_built, wear_pct, lat, lon
            FROM demolition_houses WHERE lat IS NOT NULL AND lon IS NOT NULL
        """)
        points = []
        for r in rows:
            points.append({
                "lat": float(r["lat"]), "lon": float(r["lon"]),
                "address": r["address"], "district": r["district"],
                "apartments": r["apartments"], "demolish_year": r["demolish_year"],
                "year_built": r["year_built"],
                "wear_pct": float(r["wear_pct"]) if r["wear_pct"] is not None else None,
            })
        return JSONResponse({"points": points})

    @router.get("/admin/api/crime-hexes")
    async def crime_hexes_api(request: Request, days: int | None = None):
        """Тепловая карта преступности (см. задачу) — krisha.kz/ms/geodata/crime,
        собрано в crime_incidents (crime_collect.py). Гексы 150м (та же
        сетка-подход, что и population/transport-hexes) — сырых точек за
        2+ года набирается тысячи, гексы читаемее и легче для карты.
        days — опциональное окно (последние N дней), по умолчанию вся история."""
        from bot.db.pg import fetch as pg_fetch
        from bot.core.hexgrid import hex_id, hex_center
        where = "1=1"
        params: list = []
        if days:
            where = "date_excitation >= (now() - ($1 || ' days')::interval)::date"
            params.append(str(days))
        rows = await pg_fetch(f"""
            SELECT lat, lon, hard_code FROM crime_incidents WHERE {where}
        """, *params)
        if not rows:
            return JSONResponse({"hexes": []})
        EDGE_M = 150.0
        cells: dict[str, dict] = {}
        # hard_code — категория тяжести 0-4 (0 — мелкое хищение/побои,
        # 4 — убийство/изнасилование, проверено на реальных crime_title).
        # Раньше count и тяжесть схлопывались в один "weight" — гекс с
        # многими мелкими нарушениями и гекс с одним тяжким могли давать
        # похожий score и красились одинаково, хотя ситуации разные (задача
        # "палитра больше — не только количество, но и тяжесть"). Отдаём
        # count и avg_severity ОТДЕЛЬНО — фронт красит по двум измерениям
        # (оттенок = тяжесть, интенсивность/непрозрачность = количество).
        for r in rows:
            hid = hex_id(float(r["lat"]), float(r["lon"]), EDGE_M)
            cell = cells.setdefault(hid, {"count": 0, "severity_sum": 0.0})
            cell["count"] += 1
            cell["severity_sum"] += float(r["hard_code"] or 0)
        # Нормировка count по 90-му перцентилю, а не максимуму — та же
        # причина, что и раньше: один гекс-outlier с сотнями инцидентов
        # гасил бы визуальную разницу у всей остальной массы гексов.
        counts = sorted(c["count"] for c in cells.values())
        p90_idx = min(len(counts) - 1, int(len(counts) * 0.90))
        max_count = counts[p90_idx] or 1
        hexes = []
        for hid, cell in cells.items():
            clat, clon = hex_center(hid, EDGE_M)
            avg_severity = cell["severity_sum"] / cell["count"]
            hexes.append({
                "lat": clat, "lon": clon, "count": cell["count"],
                "avg_severity": round(avg_severity, 2),
                "count_norm": round(min(1.0, cell["count"] / max_count), 4),
                # score оставлен для обратной совместимости (сортировка) —
                # тот же блендинг, что раньше, но уже не единственный сигнал.
                "score": round((cell["count"] / max_count) * (1.0 + 0.5 * avg_severity) / 3.0, 4),
            })
        hexes.sort(key=lambda h: h["score"], reverse=True)
        return JSONResponse({"hexes": hexes})

    @router.get("/admin/api/population-hexes")
    async def population_hexes_api(request: Request):
        # Оценка плотности населения по гексам (100м, та же сетка, что и
        # hype-hexes/transport_hexes): apartment_count * оценка людей/квартиру
        # по разбивке комнатности, где она известна (housing_class_test —
        # 593 ЖК, затем homeportal_objects — 550 ЖК), иначе — усреднённая по
        # городу занятость на квартиру (см. BLENDED_OCC ниже).
        from bot.db.pg import fetch as pg_fetch
        from bot.core.hexgrid import hex_id, hex_center
        # homeportal_objects может матчиться НЕСКОЛЬКИМИ строками на один
        # complex_id (отдельные корпуса/пятна одного ЖК) — агрегируем суммой
        # ДО join'а с complexes, иначе LEFT JOIN размножит строку ЖК и
        # апартаменты/население посчитаются в разы больше реальных.
        rows = await pg_fetch("""
            SELECT c.id, c.name, c.lat, c.lon,
                   hc.apartment_count, hc.entrances, hc.rooms_1, hc.rooms_2, hc.rooms_3, hc.rooms_4,
                   ho.apartments_total,
                   ho.rooms_1 AS h_rooms_1, ho.rooms_2 AS h_rooms_2,
                   ho.rooms_3 AS h_rooms_3, ho.rooms_4 AS h_rooms_4,
                   COALESCE(cts.floors_total, agg.floors_total) AS floors_total
            FROM complexes c
            LEFT JOIN housing_class_test hc ON hc.complex_id = c.id
            LEFT JOIN complex_tech_specs cts ON cts.complex_id = c.id
            LEFT JOIN (
                SELECT lower(trim(complex_name)) AS key, MAX(floors_total) AS floors_total
                FROM apartment_listings WHERE complex_name IS NOT NULL
                GROUP BY lower(trim(complex_name))
            ) agg ON agg.key = lower(trim(c.name))
            LEFT JOIN (
                SELECT matched_complex_id,
                       SUM(apartments_total) AS apartments_total,
                       SUM(rooms_1) AS rooms_1, SUM(rooms_2) AS rooms_2,
                       SUM(rooms_3) AS rooms_3, SUM(rooms_4) AS rooms_4
                FROM homeportal_objects
                WHERE matched_complex_id IS NOT NULL
                GROUP BY matched_complex_id
            ) ho ON ho.matched_complex_id = c.id
            WHERE c.lat IS NOT NULL AND c.lon IS NOT NULL
        """)
        # Точечные оценки людей/квартиру по числу комнат — нижняя граница
        # диапазонов, которые задал пользователь (1к≈3, 2к≈4-5, 3к≈6+, 4к+≈8+):
        # 1->3, 2->4, 3->6, 4+->8.
        OCC = {1: 3, 2: 4, 3: 6, 4: 8}
        # Усреднённая занятость/квартиру по городскому распределению комнатности
        # (apartment_listings.rooms, посчитано отдельно): 1к 10046, 2к 15587,
        # 3к 9614, 4+к 2327 -> (10046*3+15587*4+9614*6+2327*8)/37574 ≈ 4.49.
        # Используется, если для ЖК нет разбивки по комнатам вообще.
        BLENDED_OCC = 4.49
        # Защита от выбросов в источнике (напр. apartment_count=4000 у одного
        # ЖК в housing_class_test — явно ошибка парсинга/ввода, не реальный
        # ЖК на 4000 квартир): режем apartment_count разумным потолком перед
        # оценкой населения, а не отбрасываем ЖК целиком.
        MAX_PLAUSIBLE_APT = 1500
        EDGE_M = 100.0
        # KDE-сглаживание (вместо дискретного суммирования по гексу, где
        # лежит ЖК): каждая точка ЖК "размазывается" гауссовым ядром по
        # окрестности, значения соседних ЖК складываются. Убирает дырки
        # между соседними гексами (пустыри в Нурлы Жер/Есиле были видны как
        # "пусто" даже вплотную к плотной застройке) и точечные пятна ровно
        # на месте ЖК. BW_M — стандартное отклонение ядра, подобрано под
        # масштаб застройки Астаны (500-1000м по рекомендации).
        import math
        import numpy as np
        from bot.core.hexgrid import _to_xy, _LAT0, _LON0, _M_PER_DEG_LAT, _M_PER_DEG_LON
        BW_M = 700.0
        pts: list[tuple[float, float, float, str]] = []  # x, y, pop, name
        considered = 0
        for r in rows:
            pop = None
            r1, r2, r3, r4 = r["rooms_1"], r["rooms_2"], r["rooms_3"], r["rooms_4"]
            hr1, hr2, hr3, hr4 = r["h_rooms_1"], r["h_rooms_2"], r["h_rooms_3"], r["h_rooms_4"]
            # Общее известное число квартир в ЖК (из krisha-парсинга или
            # homeportal) — используется и как самостоятельная оценка (с
            # усреднённой занятостью), и как "потолок доверия" разбивке по
            # комнатам ниже.
            apt_total = None
            if r["apartment_count"]:
                apt_total = min(r["apartment_count"], MAX_PLAUSIBLE_APT)
            elif r["apartments_total"]:
                apt_total = min(r["apartments_total"], MAX_PLAUSIBLE_APT)
            rooms_sum = rooms_pop = None
            if any(v for v in (r1, r2, r3, r4)):
                rooms_sum = (r1 or 0) + (r2 or 0) + (r3 or 0) + (r4 or 0)
                rooms_pop = (r1 or 0) * OCC[1] + (r2 or 0) * OCC[2] + (r3 or 0) * OCC[3] + (r4 or 0) * OCC[4]
            elif any(v for v in (hr1, hr2, hr3, hr4)):
                rooms_sum = (hr1 or 0) + (hr2 or 0) + (hr3 or 0) + (hr4 or 0)
                rooms_pop = (hr1 or 0) * OCC[1] + (hr2 or 0) * OCC[2] + (hr3 or 0) * OCC[3] + (hr4 or 0) * OCC[4]
            if rooms_sum and apt_total and rooms_sum < 0.6 * apt_total:
                # Разбивка по комнатам покрывает МЕНЬШЕ 60% от известного
                # общего числа квартир в ЖК (напр. Europe City: apartment_
                # count=520 из krisha, но rooms_1..4 в housing_class_test
                # описывают только 37 из них) — явно неполные данные по
                # комнатности. Раньше такая неполная разбивка молча брала
                # приоритет над apartment_count и население ЖК занижалось
                # в разы. Экстраполируем известное распределение комнат на
                # весь apt_total, а не отбрасываем его.
                pop = rooms_pop * (apt_total / rooms_sum)
            elif rooms_pop:
                pop = rooms_pop
            elif apt_total:
                pop = apt_total * BLENDED_OCC
            elif r["entrances"] and r["floors_total"]:
                # Ни разбивки по комнатам, ни apartment_count — но есть
                # подъезды (вводятся вручную на вкладке "Класс жилья") и
                # этажность из объявлений/тех.паспорта. Оцениваем квартиры
                # как подъезды × этажи × ~4.5 кв./этаж (середина диапазона
                # 4-5, который пользователь называл как типичный для
                # Астаны) — грубее, чем прямой apartment_count, но заметно
                # точнее, чем считать такой ЖК пустым местом на карте.
                apt_est = min(r["entrances"] * r["floors_total"] * 4.5, MAX_PLAUSIBLE_APT)
                pop = apt_est * BLENDED_OCC
            if not pop:
                continue
            # Санити-проверка на пределы Астаны — см. комментарий у
            # старых пятиэтажек ниже (защита от одной плохой координаты,
            # раздувающей bbox KDE-сетки и вешающей веб-процесс).
            if not (50.0 < r["lat"] < 53.0 and 69.0 < r["lon"] < 73.0):
                continue
            considered += 1
            x, y = _to_xy(float(r["lat"]), float(r["lon"]))
            pts.append((x, y, float(pop), r["name"]))

        # ── Старые пятиэтажки (не ЖК — не попадают в таблицу complexes
        # вообще, поэтому выше их население никогда не считалось) ─────────
        # Задача: "если это пятиэтажный дом то в нём 4 подъезда, ~1950-1980
        # годов, по 2 квартиры на этаж — то есть примерно 80 квартир,
        # половина однушки, половина двушки. + в Казахстане большие семьи,
        # ~280 человек в одной пятиэтажке." Источник — обычные адреса из
        # apartment_listings БЕЗ привязки к ЖК (после стадии геопривязки по
        # адресу это как раз "старый дом" случай, см. bot.core.rebind) и с
        # floors_total=5 (сигнатура сталинки/хрущёвки/панельки).
        # Санити-проверка на пределы Астаны (та же логика, что в
        # bot.core.rebind.geocode_missing_coords) — БАГ (найден на живых
        # данных): без неё один плохо сгеокодированный адрес с lon≈57
        # (за 1000+ км от Астаны) растягивал bbox KDE-сетки в тысячи раз и
        # вешал весь веб-процесс (100% CPU не отвечал ни на один запрос,
        # ловил только рестартом сервиса) — координаты объявления/адреса не
        # проверялись на разумность нигде на этом пути.
        old_bldg_rows = await pg_fetch(r"""
            SELECT lower(trim(regexp_replace(address, '\s*—.*$', ''))) AS naddr,
                   AVG(lat) AS lat, AVG(lon) AS lon, MIN(address) AS address
            FROM apartment_listings
            WHERE (complex_name IS NULL OR btrim(complex_name) = '')
              AND address IS NOT NULL AND btrim(address) != ''
              AND floors_total = 5
              AND lat BETWEEN 50.0 AND 53.0 AND lon BETWEEN 69.0 AND 73.0
            GROUP BY naddr
        """)
        FIVE_STORY_APT_TOTAL = 80  # 4 подъезда × 2 кв./этаж × 5 этажей
        five_story_pop = (FIVE_STORY_APT_TOTAL / 2) * OCC[1] + (FIVE_STORY_APT_TOTAL / 2) * OCC[2]  # 40*3 + 40*4 = 280
        old_considered = 0
        for r in old_bldg_rows:
            if r["lat"] is None or r["lon"] is None:
                continue
            old_considered += 1
            x, y = _to_xy(float(r["lat"]), float(r["lon"]))
            pts.append((x, y, five_story_pop, r["address"]))
        considered += old_considered

        if not pts:
            return JSONResponse({"hexes": [], "complexes_considered": 0})
        xs = np.array([p[0] for p in pts])
        ys = np.array([p[1] for p in pts])
        pops = np.array([p[2] for p in pts])
        names = [p[3] for p in pts]

        # Гекс-сетка (осевые q,r), покрывающая bbox точек + запас в 3*BW_M
        # (радиус, за которым вклад гауссианы уже пренебрежимо мал).
        pad = 3 * BW_M
        xmin, xmax = xs.min() - pad, xs.max() + pad
        ymin, ymax = ys.min() - pad, ys.max() + pad
        sqrt3 = math.sqrt(3)
        qs_corner, rs_corner = [], []
        for cx in (xmin, xmax):
            for cy in (ymin, ymax):
                qs_corner.append((sqrt3 / 3 * cx - 1 / 3 * cy) / EDGE_M)
                rs_corner.append((2 / 3 * cy) / EDGE_M)
        qmin, qmax = int(math.floor(min(qs_corner))) - 2, int(math.ceil(max(qs_corner))) + 2
        rmin, rmax = int(math.floor(min(rs_corner))) - 2, int(math.ceil(max(rs_corner))) + 2
        qq, rr = np.meshgrid(np.arange(qmin, qmax + 1), np.arange(rmin, rmax + 1))
        qq = qq.ravel().astype(np.float64)
        rr = rr.ravel().astype(np.float64)
        cx_all = EDGE_M * sqrt3 * (qq + rr / 2)
        cy_all = EDGE_M * 1.5 * rr

        hex_area = (3 * sqrt3 / 2) * EDGE_M ** 2  # м² правильного гексагона
        norm = 1.0 / (2 * math.pi * BW_M ** 2)
        two_bw2 = 2 * BW_M ** 2
        cutoff2 = pad ** 2  # точки дальше 3*BW_M вклада почти не дают

        CHUNK = 4000
        pop_out = np.zeros(cx_all.shape[0])
        nearest_idx = np.full(cx_all.shape[0], -1, dtype=np.int64)
        nearest_d2 = np.full(cx_all.shape[0], np.inf)
        nearby_cnt = np.zeros(cx_all.shape[0], dtype=np.int64)
        for i0 in range(0, cx_all.shape[0], CHUNK):
            i1 = min(i0 + CHUNK, cx_all.shape[0])
            dx = cx_all[i0:i1, None] - xs[None, :]
            dy = cy_all[i0:i1, None] - ys[None, :]
            d2 = dx * dx + dy * dy
            within = d2 <= cutoff2
            contrib = np.where(within, pops[None, :] * norm * np.exp(-d2 / two_bw2), 0.0)
            pop_out[i0:i1] = contrib.sum(axis=1) * hex_area
            nearby_cnt[i0:i1] = (d2 <= (1.5 * EDGE_M) ** 2).sum(axis=1)
            nearest_idx[i0:i1] = np.argmin(d2, axis=1)
            nearest_d2[i0:i1] = np.min(d2, axis=1)

        MIN_POP = 3.0
        keep = pop_out >= MIN_POP
        idx = np.nonzero(keep)[0]
        hexes = []
        for i in idx:
            lat = _LAT0 + cy_all[i] / _M_PER_DEG_LAT
            lon = _LON0 + cx_all[i] / _M_PER_DEG_LON
            ni = nearest_idx[i]
            label = names[ni] if ni >= 0 else ""
            hexes.append({"name": label, "lat": float(lat), "lon": float(lon),
                          "population": round(float(pop_out[i])), "complexes": int(nearby_cnt[i])})
        hexes.sort(key=lambda h: h["population"], reverse=True)
        # KDE даёт плавную непрерывную поверхность -> гексов заметно больше,
        # чем ЖК в базе; ограничиваем ответ разумным потолком (карта рисует
        # верхние по значению — низкие плотности всё равно почти не видны).
        MAX_HEXES = 12000
        hexes = hexes[:MAX_HEXES]
        return JSONResponse({"hexes": hexes, "complexes_considered": considered})


    @router.get("/admin/api/geo-sales.geojson")
    async def geo_sales_geojson(request: Request, lat: float = None, lon: float = None, radius_km: float = None):
        # lat/lon/radius_km (опц.) — для мини-панели Kepler.gl в попапе объявления
        # (Task 5 в дашборде): датасет, обрезанный вокруг конкретного ЖК, а не весь
        # город, чтобы Kepler автоматически центрировался/зумился на нужный район.
        from bot.db.pg import fetch as pg_fetch
        bbox_sql = ""
        params = []
        if lat is not None and lon is not None:
            r_km = radius_km or 1.5
            dlat = r_km / 111.0
            dlon = r_km / (111.0 * max(0.1, __import__("math").cos(__import__("math").radians(lat))))
            bbox_sql = "AND lat BETWEEN $1 AND $2 AND lon BETWEEN $3 AND $4"
            params = [lat - dlat, lat + dlat, lon - dlon, lon + dlon]
        rows = await pg_fetch(f"""
            SELECT id, lat, lon, price, area, rooms, district, complex_name
            FROM apartment_listings
            WHERE is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
              AND lat IS NOT NULL AND lon IS NOT NULL AND price IS NOT NULL
              {bbox_sql}
            ORDER BY id""", *params)
        feats = []
        for r in rows:
            props = {
                "id": str(r["id"]), "price": r["price"], "rooms": r.get("rooms"),
                "district": r.get("district"), "complex": r.get("complex_name"),
            }
            if r.get("area"):
                props["price_m2"] = round(r["price"] / r["area"], 0)
            feats.append({"type": "Feature",
                          "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                          "properties": props})
        return JSONResponse(
            content={"type": "FeatureCollection", "features": feats},
            headers={"Access-Control-Allow-Origin": "*"})

    @router.get("/admin/api/geo-kepler.json")
    async def geo_kepler_map_file(request: Request, type: str = "sale",
                                   lat: float = None, lon: float = None, radius_km: float = None):
        # Полноценный экспортированный kepler.gl map-файл (данные + config в
        # одном JSON) вместо голого geojson — публичный kepler.gl/demo умеет
        # грузить такой файл через mapUrl= и ПРИМЕНЯЕТ наш config: heatmap-слой
        # (не точки) + светлый mapStyle + mapState (центр/зум сразу на район
        # объявления). Схема подобрана и проверена вручную через
        # kepler.gl/demo?mapUrl=...&: обязательно "allData" (не "rows") в
        # dataset.data, "datasets"/"config"/"info" — соседние ключи верхнего
        # уровня (не вложенный "data": {...} как в актуальных доках экспорта).
        from bot.db.pg import fetch as pg_fetch
        bbox_sql = ""
        params = []
        if lat is not None and lon is not None:
            r_km = radius_km or 1.5
            dlat = r_km / 111.0
            dlon = r_km / (111.0 * max(0.1, __import__("math").cos(__import__("math").radians(lat))))
            bbox_sql = "AND lat BETWEEN $1 AND $2 AND lon BETWEEN $3 AND $4"
            params = [lat - dlat, lat + dlat, lon - dlon, lon + dlon]
        if type == "yield":
            # Доходность (задача "Инвестиции" — тепловая карта по доходности
            # на кеплере) — тут не density-heatmap (та просто показывает, ГДЕ
            # много точек), а hexagon-слой с агрегацией colorField=yield_pct,
            # average — показывает, ГДЕ доходность выше, вне зависимости от
            # плотности предложения (это то, что нужно для "на правом берегу
            # выгоднее").
            rows_db = await pg_fetch(f"""
                SELECT lat, lon, yield_pct, rooms FROM apartment_listings
                WHERE lat IS NOT NULL AND lon IS NOT NULL AND yield_pct IS NOT NULL
                  AND is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
                  {bbox_sql}
                ORDER BY id""", *params)
            data_rows = [[float(r["lat"]), float(r["lon"]), float(r["yield_pct"]), r.get("rooms")] for r in rows_db]
            fields = [
                {"name": "lat", "format": "", "type": "real"},
                {"name": "lon", "format": "", "type": "real"},
                {"name": "yield_pct", "format": "", "type": "real"},
                {"name": "rooms", "format": "", "type": "integer"},
            ]
            content = {
                "datasets": [{
                    "version": "v1",
                    "data": {"id": "listings", "label": "listings", "color": [255, 153, 31],
                              "allData": data_rows, "fields": fields},
                }],
                "config": {"version": "v1", "config": {
                    "visState": {"layers": [{
                        "id": "hexlayer1", "type": "hexagon",
                        "config": {
                            "dataId": "listings", "label": "Доходность",
                            "columns": {"lat": "lat", "lng": "lon"},
                            "isVisible": True,
                            "visConfig": {
                                "opacity": 0.8, "worldUnitSize": 0.5, "coverage": 1,
                                "colorRange": {
                                    "name": "ColorBrewer RdYlGn-6", "type": "diverging", "category": "ColorBrewer",
                                    "colorMap": None,
                                    "colors": ["#a50026", "#f46d43", "#fee08b", "#d9ef8b", "#66bd63", "#006837"],
                                },
                                "colorAggregation": "average",
                            },
                            "visualChannels": {
                                "colorField": {"name": "yield_pct", "type": "real"},
                                "colorScale": "quantile",
                            },
                        },
                    }]},
                    "mapState": {
                        "latitude": lat if lat is not None else 51.128,
                        "longitude": lon if lon is not None else 71.43,
                        "zoom": 14 if lat is not None else 11,
                    },
                    "mapStyle": {"styleType": "light"},
                }},
                "info": {"app": "kepler.gl", "created_at": "2026-08-06T00:00:00.000Z"},
            }
            return JSONResponse(content=content, headers={"Access-Control-Allow-Origin": "*"})

        table = "rental_listings" if type == "rental" else "apartment_listings"
        active_sql = "AND is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE" if type != "rental" else ""
        rows_db = await pg_fetch(f"""
            SELECT lat, lon, price, rooms FROM {table}
            WHERE lat IS NOT NULL AND lon IS NOT NULL AND price IS NOT NULL
              {active_sql} {bbox_sql}
            ORDER BY id""", *params)
        data_rows = [[float(r["lat"]), float(r["lon"]), r["price"], r.get("rooms")] for r in rows_db]
        fields = [
            {"name": "lat", "format": "", "type": "real"},
            {"name": "lon", "format": "", "type": "real"},
            {"name": "price", "format": "", "type": "integer"},
            {"name": "rooms", "format": "", "type": "integer"},
        ]
        content = {
            "datasets": [{
                "version": "v1",
                "data": {"id": "listings", "label": "listings", "color": [255, 153, 31],
                          "allData": data_rows, "fields": fields},
            }],
            "config": {"version": "v1", "config": {
                "visState": {"layers": [{
                    "id": "hmlayer1", "type": "heatmap",
                    "config": {
                        "dataId": "listings", "label": "Цены",
                        "columns": {"lat": "lat", "lng": "lon"},
                        "isVisible": True,
                        "visConfig": {"opacity": 0.75, "radius": 22},
                    },
                }]},
                "mapState": {
                    "latitude": lat if lat is not None else 51.128,
                    "longitude": lon if lon is not None else 71.43,
                    "zoom": 14 if lat is not None else 11,
                },
                "mapStyle": {"styleType": "light"},
            }},
            "info": {"app": "kepler.gl", "created_at": "2026-08-06T00:00:00.000Z"},
        }
        return JSONResponse(content=content, headers={"Access-Control-Allow-Origin": "*"})

    @router.get("/admin/api/geo-rentals.geojson")
    async def geo_rentals_geojson(request: Request, lat: float = None, lon: float = None, radius_km: float = None):
        # Аналог geo-sales.geojson, но по rental_listings — для переключателя
        # "Продажа"/"Аренда" в Kepler-панели попапа (Task 5).
        from bot.db.pg import fetch as pg_fetch
        bbox_sql = ""
        params = []
        if lat is not None and lon is not None:
            r_km = radius_km or 1.5
            dlat = r_km / 111.0
            dlon = r_km / (111.0 * max(0.1, __import__("math").cos(__import__("math").radians(lat))))
            bbox_sql = "AND lat BETWEEN $1 AND $2 AND lon BETWEEN $3 AND $4"
            params = [lat - dlat, lat + dlat, lon - dlon, lon + dlon]
        rows = await pg_fetch(f"""
            SELECT id, lat, lon, price, area, rooms, district
            FROM rental_listings
            WHERE lat IS NOT NULL AND lon IS NOT NULL AND price IS NOT NULL
              {bbox_sql}
            ORDER BY id""", *params)
        feats = []
        for r in rows:
            props = {
                "id": str(r["id"]), "price": r["price"], "rooms": r.get("rooms"),
                "district": r.get("district"),
            }
            if r.get("area"):
                props["price_m2"] = round(r["price"] / r["area"], 0)
            feats.append({"type": "Feature",
                          "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                          "properties": props})
        return JSONResponse(
            content={"type": "FeatureCollection", "features": feats},
            headers={"Access-Control-Allow-Origin": "*"})

    @router.post("/admin/api/parse-settings")
    async def parse_settings_api(request: Request):
        from bot.db.pg import fetch as pg_fetch
        body = await request.json()
        for key, val in (("delay", body.get("delay")), ("batch", body.get("batch")),
                         ("enabled", body.get("enabled"))):
            if val is not None:
                await pg_fetch("""INSERT INTO parse_settings (key, value, updated_at)
                                  VALUES ('krisha_' || $1, $2, now())
                                  ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()""",
                               key, str(val))
        for key in ("korter_interval_h", "homsters_interval_h"):
            val = body.get(key)
            if val is not None:
                try:
                    val = max(1, int(val))
                except (TypeError, ValueError):
                    return JSONResponse({"ok": False, "error": f"{key}: ожидается число часов"})
                await pg_fetch("""INSERT INTO parse_settings (key, value, updated_at)
                                  VALUES ($1, $2, now())
                                  ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()""",
                               key, str(val))
        return JSONResponse({"ok": True})

    @router.get("/admin/api/geo-sales")
    async def geo_sales_api(request: Request):
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT id, lat, lon, price, area, rooms, district, complex_name
            FROM apartment_listings
            WHERE is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
              AND lat IS NOT NULL AND lon IS NOT NULL AND price IS NOT NULL
            ORDER BY id""")
        points = []
        for r in rows:
            price_m2 = round(r["price"] / r["area"], 0) if r.get("area") else None
            points.append({
                "id": r["id"], "lat": r["lat"], "lon": r["lon"],
                "price": r["price"], "price_m2": price_m2, "rooms": r.get("rooms"),
                "district": r.get("district"), "complex": r.get("complex_name"),
            })
        return JSONResponse({"points": points, "count": len(points)})

    @router.get("/admin/api/photo-analysis")
    async def photo_analysis_api(request: Request, days: int = 3):
        days = days if days in (1, 3, 7, 30, 90) else 3
        from bot.db.pg import fetch as pg_fetch, fetchval as pg_fv
        processed = await pg_fv("SELECT count(*) FROM apartment_listings WHERE floorplan_checked_at IS NOT NULL") or 0
        photos = await pg_fv("SELECT count(*) FROM listing_floorplans") or 0
        floorplans = await pg_fv("SELECT count(*) FROM listing_floorplans WHERE is_floorplan") or 0
        queue = await pg_fv("SELECT count(*) FROM apartment_listings WHERE floorplan_checked_at IS NULL AND photos IS NOT NULL AND photos::text != '[]' AND is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE") or 0
        # При days>7 часовая гранулярность даёт слишком много точек для
        # линейного графика — переключаемся на дневную (тот же паттерн, что
        # у остальных time-series графиков в проекте).
        bucket = "hour" if days <= 7 else "day"
        hourly = await pg_fetch(f"""
            SELECT date_trunc('{bucket}', checked_at) AS ts, count(*) AS photos, count(*) FILTER (WHERE is_floorplan) AS fps
            FROM listing_floorplans
            WHERE checked_at > now() - ($1 || ' days')::interval
            GROUP BY 1 ORDER BY 1""", str(days))
        hourly = [dict(r) for r in hourly]
        for h in hourly:
            if h.get("ts") is not None:
                h["ts"] = str(h["ts"])
        daily = await pg_fetch("SELECT floorplan_checked_at::date AS d, count(*) AS listings FROM apartment_listings WHERE floorplan_checked_at IS NOT NULL GROUP BY 1 ORDER BY 1")
        daily = [dict(r) for r in daily]
        for d in daily:
            if d.get("d") is not None:
                d["d"] = str(d["d"])
        # БАГ (найден при расследовании "последние планы от 2 августа, хотя
        # сегодня 6-е"): DISTINCT ON (l.id) обязан начинать ORDER BY с l.id
        # (требование Postgres) — LIMIT 10 в итоге брал первые 10 строк по
        # ПОРЯДКУ ID ОБЪЯВЛЕНИЯ, а не по свежести, checked_at вообще не
        # влиял на то, какие 10 строк попадут в выдачу. Внешний ORDER BY
        # поверх подзапроса — DISTINCT ON схлопывает дубли (один план на
        # объявление, лучший по score), сортировка по реальной свежести
        # применяется уже после.
        recent = await pg_fetch("""
            SELECT * FROM (
                SELECT DISTINCT ON (l.id) l.id, fp.photo_url, fp.floorplan_score::float AS score,
                       l.title, l.address, fp.checked_at
                FROM listing_floorplans fp
                JOIN apartment_listings l ON l.id = fp.listing_id
                WHERE fp.is_floorplan
                ORDER BY l.id, fp.floorplan_score DESC, fp.id DESC
            ) sub
            ORDER BY checked_at DESC LIMIT 10""")
        recent = [dict(r) for r in recent]
        for r in recent:
            if r.get("checked_at") is not None:
                r["checked_at"] = str(r["checked_at"])
        return JSONResponse({"stats": {"processed": processed, "photos": photos, "floorplans": floorplans, "queue": queue}, "hourly": hourly, "daily": daily, "recent": recent})

    @router.get("/admin/api/transport-hexes")
    async def transport_hexes_api(request: Request):
        import psycopg2
        db = psycopg2.connect(_load_db_url("krisha_bot"))
        cur = db.cursor()
        cur.execute("SELECT lat, lon, score, dist_lrt, dist_bus, dist_road, dist_junction FROM transport_hexes ORDER BY score DESC")
        cols = [d[0] for d in cur.description]
        hexes = [dict(zip(cols, row)) for row in cur.fetchall()]
        cur.close(); db.close()
        return JSONResponse({"hexes": hexes})



    # ── Кеширующий прокси для фото объявлений/ЖК ────────────────────────────
    # Krisha сама раздаёт фото через свой CDN (kcdn.online) — мы их не
    # массово скачиваем, а кэшируем НА ЛЕТУ по факту первого просмотра
    # (первый запрос идёт на источник и сохраняется на диск, повторные —
    # сразу с диска). Не открытый прокси: домен строго из allowlist.

    @router.get("/img-proxy")
    async def img_proxy(request: Request, u: str):
        fpath = _photo_cache_path(u)
        if not fpath:
            return JSONResponse({"error": "host not allowed"}, status_code=400)

        if not os.path.exists(fpath):
            ok = await prewarm_photo_cache(u)
            if not ok:
                # источник недоступен — отдаём оригинал напрямую, чтобы фото
                # хотя бы показалось (не кэшируем неудачу)
                return RedirectResponse(url=u)

        return FileResponse(fpath, headers={"Cache-Control": "public, max-age=2592000"})

    # ── Настройки ─────────────────────────────────────────────────────────

    # ── Мониторинг сервера/проекта (CPU/память/диск/размер) ────────────────

    @router.get("/admin/api/system-stats")
    async def system_stats_live(request: Request):
        """Мгновенный снимок для живого обновления на /admin/settings (опрос
        раз в несколько секунд с клиента) — см. bot/core/system_stats.py."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.core.system_stats import read_live_stats
        from bot.db.pg import fetchrow as pg_fr
        live = read_live_stats()
        last_project = await pg_fr(
            "SELECT project_size_gb, at FROM system_stats_history ORDER BY at DESC LIMIT 1")
        return JSONResponse({
            **live,
            "project_size_gb": float(last_project["project_size_gb"]) if last_project else None,
            "project_size_at": last_project["at"].strftime("%d.%m.%Y %H:%M") if last_project else None,
        })

    @router.get("/admin/api/system-stats-history")
    async def system_stats_history_api(request: Request, hours: int = 24):
        """История снимков (раз в цикл парсера продаж) — для графика на
        /admin/settings."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT at, cpu_pct, mem_pct, disk_pct, project_size_gb
            FROM system_stats_history
            WHERE at > now() - ($1 || ' hours')::interval
            ORDER BY at ASC
        """, str(hours))
        return JSONResponse({"points": [{
            "at": r["at"].strftime("%d.%m %H:%M"),
            "cpu_pct": r["cpu_pct"], "mem_pct": r["mem_pct"], "disk_pct": r["disk_pct"],
            "project_size_gb": float(r["project_size_gb"]) if r["project_size_gb"] is not None else None,
        } for r in rows]})

    # ── Личный кабинет посетителя сайта (вход через Telegram, см.
    # bot/core/site_auth.py + service_site_bot.py) — отдельно от admin_auth ──

    def _site_session_cookie(request: Request) -> str | None:
        return request.cookies.get("site_session")

    @router.get("/cabinet", response_class=HTMLResponse)
    async def cabinet_page(request: Request):
        from bot.core.site_auth import get_user_by_session, list_favorites, list_favorite_complexes
        user = await get_user_by_session(_site_session_cookie(request))
        favorites = await list_favorites(user["user_id"]) if user else []
        favorite_complexes = await list_favorite_complexes(user["user_id"]) if user else []
        return templates.TemplateResponse("cabinet.html", {
            "request": request, "user": user, "favorites": favorites,
            "favorite_complexes": favorite_complexes,
            "bot_username": os.getenv("SITE_BOT_USERNAME", "nik_us_bot"),
        })

    @router.get("/favorites", response_class=HTMLResponse)
    async def favorites_page(request: Request):
        """Отдельная страница избранного (карточки + сравнение таблицей) —
        раньше избранное было видно только внутри /cabinet одним списком."""
        from bot.core.site_auth import get_user_by_session, list_favorites, list_favorite_complexes
        user = await get_user_by_session(_site_session_cookie(request))
        favorites = await list_favorites(user["user_id"]) if user else []
        favorite_complexes = await list_favorite_complexes(user["user_id"]) if user else []
        return templates.TemplateResponse("favorites.html", {
            "request": request, "user": user, "favorites": favorites,
            "favorite_complexes": favorite_complexes,
        })

    # ── Избранные ЖК — те же ручки, что у избранных квартир, но по complex_id ──
    @router.get("/api/favorite-complexes/ids")
    async def api_favorite_complex_ids(request: Request, ids: str = ""):
        from bot.core.site_auth import get_user_by_session, is_favorite_complex_ids
        user = await get_user_by_session(_site_session_cookie(request))
        if not user:
            return JSONResponse({"ids": []})
        complex_ids = [int(i) for i in ids.split(",") if i.strip().isdigit()]
        found = await is_favorite_complex_ids(user["user_id"], complex_ids)
        return JSONResponse({"ids": list(found)})

    @router.post("/api/favorite-complexes/{complex_id}")
    async def api_add_favorite_complex(request: Request, complex_id: int):
        from bot.core.site_auth import get_user_by_session, add_favorite_complex
        user = await get_user_by_session(_site_session_cookie(request))
        if not user:
            return JSONResponse({"error": "auth"}, status_code=401)
        await add_favorite_complex(user["user_id"], complex_id)
        return JSONResponse({"ok": True})

    @router.delete("/api/favorite-complexes/{complex_id}")
    async def api_remove_favorite_complex(request: Request, complex_id: int):
        from bot.core.site_auth import get_user_by_session, remove_favorite_complex
        user = await get_user_by_session(_site_session_cookie(request))
        if not user:
            return JSONResponse({"error": "auth"}, status_code=401)
        await remove_favorite_complex(user["user_id"], complex_id)
        return JSONResponse({"ok": True})

    # ── Избранные зоны — уведомления по изменению цен внутри зоны ──────────
    @router.get("/api/favorite-zones/ids")
    async def api_favorite_zone_ids(request: Request, ids: str = ""):
        from bot.core.site_auth import get_user_by_session, is_favorite_zone_ids
        user = await get_user_by_session(_site_session_cookie(request))
        if not user:
            return JSONResponse({"ids": []})
        zone_ids = [int(i) for i in ids.split(",") if i.strip().isdigit()]
        found = await is_favorite_zone_ids(user["user_id"], zone_ids)
        return JSONResponse({"ids": list(found)})

    @router.post("/api/favorite-zones/{zone_id}")
    async def api_add_favorite_zone(request: Request, zone_id: int):
        from bot.core.site_auth import get_user_by_session, add_favorite_zone
        user = await get_user_by_session(_site_session_cookie(request))
        if not user:
            return JSONResponse({"error": "auth"}, status_code=401)
        await add_favorite_zone(user["user_id"], zone_id)
        return JSONResponse({"ok": True})

    @router.delete("/api/favorite-zones/{zone_id}")
    async def api_remove_favorite_zone(request: Request, zone_id: int):
        from bot.core.site_auth import get_user_by_session, remove_favorite_zone
        user = await get_user_by_session(_site_session_cookie(request))
        if not user:
            return JSONResponse({"error": "auth"}, status_code=401)
        await remove_favorite_zone(user["user_id"], zone_id)
        return JSONResponse({"ok": True})

    @router.post("/api/auth/start")
    async def api_auth_start(request: Request):
        from bot.core.site_auth import create_login_token
        token = await create_login_token()
        bot_username = os.getenv("SITE_BOT_USERNAME", "nik_us_bot")
        return JSONResponse({
            "token": token,
            "deep_link": f"https://t.me/{bot_username}?start={token}",
        })

    @router.get("/api/auth/poll")
    async def api_auth_poll(request: Request, token: str):
        from bot.core.site_auth import get_token_status, create_session
        status = await get_token_status(token)
        if not status:
            return JSONResponse({"status": "not_found"})
        if status["status"] == "verified":
            session_id = await create_session(status["telegram_id"])
            resp = JSONResponse({"status": "verified"})
            resp.set_cookie("site_session", session_id, httponly=True, max_age=180 * 86400)
            return resp
        return JSONResponse({"status": status["status"]})

    @router.post("/api/auth/logout")
    async def api_auth_logout(request: Request):
        from bot.core.site_auth import destroy_session
        await destroy_session(_site_session_cookie(request))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("site_session")
        return resp

    @router.post("/api/me")
    async def api_update_profile(request: Request):
        from bot.core.site_auth import get_user_by_session, update_profile
        user = await get_user_by_session(_site_session_cookie(request))
        if not user:
            return JSONResponse({"error": "auth"}, status_code=401)
        body = await request.json()
        await update_profile(
            user["user_id"],
            (body.get("full_name") or "").strip() or None,
            (body.get("email") or "").strip() or None,
            body.get("notify_frequency") if body.get("notify_frequency") in ("daily", "weekly", "off") else None,
        )
        return JSONResponse({"ok": True})

    @router.get("/api/favorites")
    async def api_list_favorites(request: Request):
        from bot.core.site_auth import get_user_by_session, list_favorites
        user = await get_user_by_session(_site_session_cookie(request))
        if not user:
            return JSONResponse({"error": "auth"}, status_code=401)
        favs = await list_favorites(user["user_id"])
        return JSONResponse({"favorites": favs})

    @router.get("/api/favorites/ids")
    async def api_favorite_ids(request: Request, ids: str = ""):
        """Проверить, какие из перечисленных id уже в избранном — для звёздочек
        на карточках дашборда (публичный запрос, но без user'а всегда пусто)."""
        from bot.core.site_auth import get_user_by_session, is_favorite_ids
        user = await get_user_by_session(_site_session_cookie(request))
        if not user:
            return JSONResponse({"ids": []})
        listing_ids = [i for i in ids.split(",") if i]
        found = await is_favorite_ids(user["user_id"], listing_ids)
        return JSONResponse({"ids": list(found)})

    @router.post("/api/favorites/{listing_id}")
    async def api_add_favorite(request: Request, listing_id: str):
        from bot.core.site_auth import get_user_by_session, add_favorite
        user = await get_user_by_session(_site_session_cookie(request))
        if not user:
            return JSONResponse({"error": "auth"}, status_code=401)
        await add_favorite(user["user_id"], listing_id)
        return JSONResponse({"ok": True})

    @router.delete("/api/favorites/{listing_id}")
    async def api_remove_favorite(request: Request, listing_id: str):
        from bot.core.site_auth import get_user_by_session, remove_favorite
        user = await get_user_by_session(_site_session_cookie(request))
        if not user:
            return JSONResponse({"error": "auth"}, status_code=401)
        await remove_favorite(user["user_id"], listing_id)
        return JSONResponse({"ok": True})

    # ── Админ: управление пользователями сайта (отдельно от /admin/users —
    # те аккаунты для входа в саму админку) ────────────────────────────────

    @router.get("/admin/site-users", response_class=HTMLResponse)
    async def admin_site_users_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.core.site_auth import list_site_users
        users = await list_site_users()
        return templates.TemplateResponse("site_users.html", {
            "request": request, "site_users": users,
        })

    @router.post("/admin/api/site-users/{user_id}/block")
    async def admin_block_site_user(request: Request, user_id: int, blocked: bool = True):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.core.site_auth import set_user_blocked
        await set_user_blocked(user_id, blocked)
        return JSONResponse({"ok": True})

    @router.delete("/admin/api/site-users/{user_id}")
    async def admin_delete_site_user(request: Request, user_id: int):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.core.site_auth import delete_site_user
        await delete_site_user(user_id)
        return JSONResponse({"ok": True})

    @router.get("/admin/entity-ids", response_class=HTMLResponse)
    async def entity_ids_page(request: Request):
        """АЙДИ — обзор entity resolution, фаза 1 (docs/entity_resolution_plan.md):
        сколько ЖК и юнитов покрыто постоянным ID + связями с источниками,
        как считается уверенность матчинга, график роста покрытия во времени."""
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import fetchrow as pg_fetchrow, fetch as pg_fetch
        from bot.core.entity_resolution import (
            AUTO_MATCH_THRESHOLD, REVIEW_QUEUE_THRESHOLD,
            GEO_MATCH_RADIUS_M, _W_NAME_EXACT, _W_GEO, _W_DEVELOPER,
        )

        totals = await pg_fetchrow("""
            SELECT
                (SELECT COUNT(*) FROM complexes
                    WHERE COALESCE(is_garbage, FALSE) = FALSE AND COALESCE(is_street, FALSE) = FALSE) AS complexes_total,
                (SELECT COUNT(DISTINCT complex_id) FROM complex_source_links) AS complexes_resolved,
                (SELECT COUNT(*) FROM newbuild_units) AS units_total,
                (SELECT COUNT(*) FROM newbuild_units u
                    WHERE EXISTS (SELECT 1 FROM complex_source_links l WHERE l.complex_id = u.complex_id)) AS units_resolved,
                (SELECT COUNT(*) FROM complex_source_links) AS links_total,
                (SELECT COUNT(DISTINCT complex_id) FROM complex_source_links
                    WHERE confidence >= 0.5 AND confidence < 0.8) AS review_queue_complexes,
                (SELECT ROUND(AVG(cnt), 2) FROM (
                    SELECT complex_id, COUNT(*) AS cnt FROM complex_source_links GROUP BY complex_id
                ) x) AS avg_sources_per_resolved
        """)
        by_source = await pg_fetch("""
            SELECT source, COUNT(*) AS n, ROUND(AVG(confidence)::numeric, 2) AS avg_conf
            FROM complex_source_links GROUP BY source ORDER BY n DESC
        """)
        by_method = await pg_fetch("""
            SELECT match_method, COUNT(*) AS n
            FROM complex_source_links GROUP BY match_method ORDER BY n DESC
        """)
        return templates.TemplateResponse("entity_ids.html", {
            "request": request,
            "totals": dict(totals) if totals else {},
            "by_source": [dict(r) for r in by_source],
            "by_method": [dict(r) for r in by_method],
            "thresholds": {
                "auto": AUTO_MATCH_THRESHOLD, "review": REVIEW_QUEUE_THRESHOLD,
                "geo_radius_m": GEO_MATCH_RADIUS_M,
                "w_name": _W_NAME_EXACT, "w_geo": _W_GEO, "w_developer": _W_DEVELOPER,
            },
        })

    @router.get("/admin/api/entity-ids/timeline")
    async def entity_ids_timeline(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch

        links_daily = await pg_fetch("""
            SELECT date_trunc('day', matched_at)::date AS d, COUNT(*) AS n
            FROM complex_source_links GROUP BY 1 ORDER BY 1
        """)
        complexes_daily = await pg_fetch("""
            SELECT d, COUNT(*) AS n FROM (
                SELECT complex_id, MIN(date_trunc('day', matched_at)::date) AS d
                FROM complex_source_links GROUP BY complex_id
            ) x GROUP BY d ORDER BY d
        """)

        def cumulative(rows):
            out, total = [], 0
            for r in rows:
                total += r["n"]
                out.append({"d": r["d"].strftime("%Y-%m-%d"), "cum": total})
            return out

        return JSONResponse({"data": {
            "links": cumulative(links_daily),
            "complexes": cumulative(complexes_daily),
        }})

    @router.post("/admin/api/site-users/{user_id}/full-access")
    async def admin_set_site_user_full_access(request: Request, user_id: int, full_access: bool = True):
        # Задача "общий доступ" (2026-08-12) — единственное место, где
        # регистрация через Telegram превращается в tier="subscriber"
        # (см. get_user_tier() выше). По умолчанию все зарегистрированные
        # остаются на публичном тире.
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.core.site_auth import set_user_full_access
        await set_user_full_access(user_id, full_access)
        return JSONResponse({"ok": True})

    @router.get("/admin/monitoring", response_class=HTMLResponse)
    async def monitoring_page(request: Request):
        # Раньше блок "Сервер и проект" жил прямо на /admin/settings —
        # вынесен на отдельную страницу, чтобы не грузить настройки лишним
        # живым polling'ом на 4 метрики каждые 5с.
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("monitoring.html", {"request": request})

    @router.get("/admin/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        await app_settings.load()
        current = app_settings.all_settings()
        groups: dict[str, list] = {}
        for key, (label, mn, mx, step, unit, group) in SLIDER_SETTINGS.items():
            groups.setdefault(group, []).append({
                "key": key, "label": label, "min": mn, "max": mx,
                "step": step, "unit": unit,
                "value": current.get(key, "0"),
                "desc": SLIDER_DESCRIPTIONS.get(key),
            })
        sliders = [s for g in groups.values() for s in g]  # обратная совместимость

        from bot.core.auth_users import ensure_seeded, list_users
        await ensure_seeded(os.getenv("ADMIN_PASSWORD", "123"))
        users = await list_users()
        current_username = request.cookies.get("admin_user") or "admin"

        return templates.TemplateResponse("settings.html", {
            "request": request,
            "sliders": sliders,
            "slider_groups": groups,
            "monetization": app_settings.get_bool("MONETIZATION_ENABLED"),
            "ai_analysis": app_settings.get_bool("AI_TEXT_ANALYSIS"),
            "deepseek_key_set": bool(__import__("os").getenv("DEEPSEEK_API_KEY")),
            "users": [dict(u) for u in users],
            "current_username": current_username,
        })

    @router.post("/admin/users-manage/create")
    async def users_manage_create(request: Request, username: str = Form(...), password: str = Form(...)):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.core.auth_users import create_user
        username = username.strip()
        if username and password:
            await create_user(username, password)
        return RedirectResponse(url="/admin/settings", status_code=302)

    @router.post("/admin/users-manage/password")
    async def users_manage_password(request: Request, user_id: int = Form(...), new_password: str = Form(...)):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.core.auth_users import set_password
        if new_password:
            await set_password(user_id, new_password)
        return RedirectResponse(url="/admin/settings", status_code=302)

    @router.post("/admin/users-manage/delete")
    async def users_manage_delete(request: Request, user_id: int = Form(...)):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.core.auth_users import delete_user
        await delete_user(user_id)
        return RedirectResponse(url="/admin/settings", status_code=302)

    @router.post("/admin/settings/save")
    async def settings_save(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        data = await request.json()
        saved = {}
        for key, value in data.items():
            if key not in SLIDER_SETTINGS:
                continue
            try:
                float(value)
            except (TypeError, ValueError):
                continue
            await app_settings.set(key, str(value))
            saved[key] = value
        logger.info("settings saved: %s", saved)
        return JSONResponse({"ok": True, "saved": saved})

    # ── Монетизация ───────────────────────────────────────────────────────

    @router.post("/admin/ai-analysis/toggle")
    async def ai_analysis_toggle(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        await app_settings.load()
        new_value = "0" if app_settings.get_bool("AI_TEXT_ANALYSIS") else "1"
        await app_settings.set("AI_TEXT_ANALYSIS", new_value)
        logger.info("AI text analysis -> %s", new_value)
        return JSONResponse({"ok": True, "enabled": new_value == "1"})

    @router.post("/admin/complex-facts/toggle")
    async def complex_facts_toggle(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        await app_settings.load()
        new_value = "0" if app_settings.get_bool("AI_COMPLEX_FACTS", True) else "1"
        await app_settings.set("AI_COMPLEX_FACTS", new_value)
        logger.info("AI complex facts -> %s", new_value)
        return JSONResponse({"ok": True, "enabled": new_value == "1"})

    @router.post("/admin/finish-classify/toggle")
    async def finish_classify_toggle(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        await app_settings.load()
        new_value = "0" if app_settings.get_bool("AI_FINISH_CLASSIFY", True) else "1"
        await app_settings.set("AI_FINISH_CLASSIFY", new_value)
        logger.info("finish classify (keywords) -> %s", new_value)
        return JSONResponse({"ok": True, "enabled": new_value == "1"})

    @router.post("/admin/floorplan-scan/toggle")
    async def floorplan_scan_toggle(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        await app_settings.load()
        new_value = "0" if app_settings.get_bool("AI_FLOORPLAN_SCAN", True) else "1"
        await app_settings.set("AI_FLOORPLAN_SCAN", new_value)
        logger.info("floorplan scan -> %s", new_value)
        return JSONResponse({"ok": True, "enabled": new_value == "1"})

    @router.post("/admin/monetization/toggle")
    async def monetization_toggle(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        await app_settings.load()
        new_value = "0" if app_settings.get_bool("MONETIZATION_ENABLED") else "1"
        await app_settings.set("MONETIZATION_ENABLED", new_value)
        logger.info("monetization -> %s", new_value)
        return JSONResponse({"ok": True, "enabled": new_value == "1"})

    # ── Управление проектом (systemd) ─────────────────────────────────────

    async def _systemctl(action: str, service: str) -> tuple[bool, str]:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "/usr/bin/systemctl", action, f"{service}.service",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode == 0, out.decode(errors="replace").strip()

    async def _is_active(service: str) -> bool:
        # "--quiet" (exit-code only) считает работающим только состояние
        # "active" — для долгих Type=oneshot юнитов (например
        # krisha-homeportal, один прогон ~15+ минут) systemd всё это время
        # показывает "activating", и is-active --quiet ошибочно говорит
        # "не работает", хотя процесс реально жив и делает дело. Читаем
        # состояние текстом и считаем работающим оба варианта.
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", f"{service}.service",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        state = out.decode().strip()
        return state in ("active", "activating")

    @router.get("/admin/project/status")
    async def project_status(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        statuses = {}
        for svc in PROJECT_SERVICES:
            statuses[svc] = await _is_active(svc)
        return JSONResponse({"services": statuses, "running": all(statuses.values())})

    @router.post("/admin/project/start")
    async def project_start(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        results = {}
        for svc in PROJECT_SERVICES:
            ok, msg = await _systemctl("start", svc)
            results[svc] = {"ok": ok, "msg": msg}
        return JSONResponse({"ok": all(r["ok"] for r in results.values()), "results": results})

    @router.post("/admin/project/stop")
    async def project_stop(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        results = {}
        for svc in PROJECT_SERVICES:
            ok, msg = await _systemctl("stop", svc)
            results[svc] = {"ok": ok, "msg": msg}
        return JSONResponse({"ok": all(r["ok"] for r in results.values()), "results": results})

    # ── /admin/parsers: единая страница со всеми парсерами/скрейперами и
    # реальными вкл/выкл переключателями (systemctl stop/start через sudo,
    # либо app_settings-флаг для скриптов без своего systemd-юнита) ────────

    # Только эти сервисы разрешено дёргать с этой страницы (белый список —
    # не даём POST'у управлять произвольным systemd-юнитом по имени).
    PARSERS_SYSTEMD = {
        "krisha-apartments": "Основной парсер объявлений о ПРОДАЖЕ с krisha.kz",
        "krisha-rental": "Парсер объявлений об АРЕНДЕ с krisha.kz",
        "krisha-korter": "Обогащение данными korter.kz (класс жилья, застройщик, район, цена/м²)",
        "krisha-homsters": "Обогащение данными homsters.kz (первичный рынок, данные по ЖК)",
        "krisha-market": "Внешние рыночные данные: ставка НБРК, депозиты КДИФ, индекс жилья stat.gov.kz",
        "krisha-viewcount": "Реальные просмотры объявлений (Playwright) на krisha.kz",
        "krisha-homeportal": "homeportal.kz — официальные данные КЖК по ЖК (долевое строительство)",
    }

    async def _parser_registry_blocks():
        """Строит список карточек парсеров (used by /admin/parsers AND
        /admin/parser — держим в одном месте, чтобы обе страницы не
        расходились со временем)."""
        from bot.db.pg import fetch as pg_fetch, fetchrow as pg_fetchrow
        await app_settings.load()

        def one(rows):
            return rows[0] if rows else {}

        blocks = []

        # 1) krisha-apartments — листинги ПРОДАЖА
        active = await _is_active("krisha-apartments")
        r = await pg_fetchrow("""SELECT
            count(*) FILTER (WHERE last_seen > now() - interval '1 hour') AS h1,
            count(*) FILTER (WHERE last_seen > now() - interval '1 day') AS d1,
            max(last_seen) AS last
            FROM apartment_listings""")
        blocks.append({
            "key": "krisha-apartments", "kind": "systemd", "active": active,
            "name": "🏠 Продажа (krisha.kz)", "desc": PARSERS_SYSTEMD["krisha-apartments"],
            "activity": f"тронуто за час: {r['h1'] or 0}, за сутки: {r['d1'] or 0}",
            "last": r["last"],
        })

        # 2) krisha-rental — листинги АРЕНДА
        active = await _is_active("krisha-rental")
        r = await pg_fetchrow("""SELECT
            count(*) FILTER (WHERE last_seen > now() - interval '1 hour') AS h1,
            count(*) FILTER (WHERE last_seen > now() - interval '1 day') AS d1,
            max(last_seen) AS last
            FROM rental_listings""")
        blocks.append({
            "key": "krisha-rental", "kind": "systemd", "active": active,
            "name": "🔑 Аренда (krisha.kz)", "desc": PARSERS_SYSTEMD["krisha-rental"],
            "activity": f"тронуто за час: {r['h1'] or 0}, за сутки: {r['d1'] or 0}",
            "last": r["last"],
        })

        # 3) krisha-korter
        active = await _is_active("krisha-korter")
        r = await pg_fetchrow("""SELECT count(*) AS cnt, max(updated_at) AS last
            FROM complexes WHERE source_info ? 'korter'""")
        blocks.append({
            "key": "krisha-korter", "kind": "systemd", "active": active,
            "name": "🏢 Korter.kz", "desc": PARSERS_SYSTEMD["krisha-korter"],
            "activity": f"ЖК с данными korter: {r['cnt'] or 0}",
            "last": r["last"],
        })

        # 4) krisha-homsters
        active = await _is_active("krisha-homsters")
        r = await pg_fetchrow("""SELECT count(*) AS cnt, max(updated_at) AS last
            FROM complexes WHERE source_info ? 'homsters'""")
        blocks.append({
            "key": "krisha-homsters", "kind": "systemd", "active": active,
            "name": "🏗 Homsters.kz", "desc": PARSERS_SYSTEMD["krisha-homsters"],
            "activity": f"ЖК с данными homsters: {r['cnt'] or 0}",
            "last": r["last"],
        })

        # 5) krisha-market
        active = await _is_active("krisha-market")
        market_updated = app_settings.get("MARKET_DATA_UPDATED_AT", None)
        blocks.append({
            "key": "krisha-market", "kind": "systemd", "active": active,
            "name": "📊 Рыночные данные", "desc": PARSERS_SYSTEMD["krisha-market"],
            "activity": f"обновлено: {market_updated or '—'}",
            "last": None,
        })

        # 6) krisha-viewcount
        active = await _is_active("krisha-viewcount")
        r = await pg_fetchrow("""SELECT
            count(*) FILTER (WHERE views_count_updated_at > now() - interval '1 day') AS d1,
            max(views_count_updated_at) AS last
            FROM apartment_listings""")
        blocks.append({
            "key": "krisha-viewcount", "kind": "systemd", "active": active,
            "name": "👁 Просмотры (Playwright)", "desc": PARSERS_SYSTEMD["krisha-viewcount"],
            "activity": f"обновлено просмотров за сутки: {r['d1'] or 0}",
            "last": r["last"],
        })

        # 7) hype_tracker/krisha_complex_scan.py — cron, флаг app_settings
        enabled = app_settings.get_bool("PARSER_KRISHA_COMPLEX_SCAN", True)
        last_log = await pg_fetchrow("SELECT ts, status FROM krisha_parse_log ORDER BY id DESC LIMIT 1")
        ok_24h = one(await pg_fetch("""SELECT count(*)::int AS n FROM krisha_parse_log
            WHERE status='ok' AND ts > now() - interval '1 day'""")).get("n", 0)
        blocks.append({
            "key": "krisha-complex-scan", "kind": "flag", "active": enabled,
            "name": "📸 ЖК по krisha.kz (cron, раз в 20 мин)",
            "desc": "Количество квартир + описание + фото ЖК со страниц комплексов на krisha.kz",
            "activity": f"успешных за сутки: {ok_24h}" + (f", последний запуск: {last_log['ts']:%d.%m %H:%M} ({last_log['status']})" if last_log else ", ещё не запускался"),
            "last": last_log["ts"] if last_log else None,
        })

        # 8) hype_tracker/homeportal_scan.py — теперь systemd timer (см. Task A)
        active = await _is_active("krisha-homeportal")
        stats = one(await pg_fetch("SELECT count(*)::int AS n, max(fetched_at) AS last FROM homeportal_objects"))
        blocks.append({
            "key": "krisha-homeportal", "kind": "systemd", "active": active,
            "name": "🏛 Homeportal.kz", "desc": PARSERS_SYSTEMD["krisha-homeportal"],
            "activity": f"объектов в БД: {stats.get('n', 0)}",
            "last": stats.get("last"),
        })

        # 9) Новостройки — прямой парсинг шахматок у застройщиков (BI Group,
        # дальше остальные из списка). Пока ручной запуск кнопкой + флаг под
        # будущий cron (см. _parser_novostroyki.html); "последний запуск" —
        # newbuild_last_scan_at свежайшего обновлённого ЖК.
        enabled_nb = app_settings.get_bool("PARSER_NEWBUILD_SCAN", True)
        nb_last = await pg_fetchrow(
            "SELECT max(newbuild_last_scan_at) AS last FROM complexes WHERE is_newbuild")
        nb_totals = one(await pg_fetch("""
            SELECT count(DISTINCT c.id)::int AS complexes,
                   count(*) FILTER (WHERE u.status IN ('available','reserved'))::int AS active,
                   count(*) FILTER (WHERE u.status = 'sold')::int AS sold
            FROM complexes c JOIN newbuild_units u ON u.complex_id = c.id
            WHERE c.is_newbuild
        """))
        blocks.append({
            "key": "novostroyki", "kind": "flag", "active": enabled_nb,
            "name": "🏗 Новостройки (застройщики напрямую)",
            "desc": "Шахматки квартир у застройщиков (BI Group и далее) — что в наличии, что уже ушло",
            "activity": (f"ЖК: {nb_totals.get('complexes', 0)}, в наличии: {nb_totals.get('active', 0)}, "
                         f"продано: {nb_totals.get('sold', 0)}"),
            "last": nb_last["last"] if nb_last else None,
        })

        return blocks

    async def _activity_over_time(table: str, ts_col: str, days: int,
                                  extra_where: str = "") -> dict:
        """Универсальная гистограмма «что и когда спарсилось» для вкладок
        /admin/parsers (Task 1: график активности на каждой вкладке парсера).
        table/ts_col/extra_where — ВСЕГДА внутренние константы из вызовов
        ниже (никогда не пользовательский ввод), поэтому f-string в SQL тут
        безопасен. bucket='hour' для 1-3 дней (иначе не видно детали),
        'day' для 7/30 (иначе тысячи точек на графике)."""
        from bot.db.pg import fetch as pg_fetch
        bucket = "hour" if days <= 3 else "day"
        fmt = "%d.%m %H:00" if bucket == "hour" else "%d.%m"
        where = f"{ts_col} > now() - ($1 || ' days')::interval"
        if extra_where:
            where += f" AND {extra_where}"
        rows = await pg_fetch(f"""
            SELECT date_trunc('{bucket}', {ts_col}) AS b, COUNT(*) AS cnt
            FROM {table}
            WHERE {where}
            GROUP BY 1 ORDER BY 1
        """, str(days))
        return {
            "bucket": bucket,
            "labels": [r["b"].strftime(fmt) for r in rows],
            "values": [r["cnt"] for r in rows],
        }

    # Табличка/колонка/фильтр для графика "что спарсилось со временем" на
    # каждой вкладке — что именно значит "спарсено" разное для каждого
    # источника (см. задачу Task 1 в тикете): апартаменты/аренда — новые
    # объявления по first_seen/found_at; korter/homsters — обогащение
    # complexes, сигнал берём из source_changes (реальные события записи,
    # точнее чем complexes.updated_at, который может тронуть и другой
    # источник); market — прогоны source_runs (см. market_data.update_all,
    # теперь тоже пишет туда); viewcount — apartment_listings.views_count_updated_at;
    # complex-scan — krisha_parse_log status='ok'; homeportal — homeportal_objects.fetched_at.
    PARSER_ACTIVITY_SPEC = {
        "krisha-apartments": ("apartment_listings", "first_seen", "", "Новые объявления о продаже (first_seen)"),
        "krisha-rental": ("rental_listings", "found_at", "", "Новые объявления об аренде (found_at)"),
        "krisha-korter": ("source_changes", "ts", "source = 'korter'", "События обогащения korter.kz (новые ЖК + изменения)"),
        "krisha-homsters": ("source_changes", "ts", "source = 'homsters'", "События обогащения homsters.kz (новые ЖК + изменения)"),
        "krisha-market": ("source_runs", "started_at", "source = 'market'", "Прогоны сбора рыночных данных (раз в ~7 дней)"),
        "krisha-viewcount": ("apartment_listings", "views_count_updated_at", "", "Обновлено просмотров объявлений"),
        "krisha-complex-scan": ("krisha_parse_log", "ts", "status = 'ok'", "Успешно спарсено ЖК со страниц krisha.kz"),
        "krisha-homeportal": ("homeportal_objects", "fetched_at", "", "Спарсено объектов homeportal.kz"),
    }

    async def _source_changes_data(source: str, days: int):
        """Данные для вкладок Korter/Homsters: прогоны (длительность полного
        обхода) и изменения (новые ЖК / изменённые параметры)."""
        from bot.db.pg import fetch as pg_fetch
        changes = await pg_fetch(
            """SELECT sc.ts, sc.complex_id,
                        COALESCE(c.name, sc.complex_name) AS complex_name,
                        sc.change_type, sc.field, sc.old_value, sc.new_value
                 FROM source_changes sc
                 LEFT JOIN complexes c ON c.id = sc.complex_id
                 WHERE sc.source = $1 AND sc.ts > now() - make_interval(days => $2)
                 ORDER BY sc.ts DESC, sc.id DESC
                 LIMIT 300""", source, days)
        runs = await pg_fetch(
            """SELECT started_at, duration_s, matched, created, changed
               FROM source_runs WHERE source = $1
               ORDER BY started_at DESC LIMIT 12""", source)
        runs_list = [dict(r) for r in runs]
        interval_row = await pg_fetch(
            "SELECT value FROM parse_settings WHERE key = $1",
            f"{source}_interval_h")
        interval_h = 120
        if interval_row and interval_row[0]["value"]:
            try:
                interval_h = max(1, int(interval_row[0]["value"]))
            except (TypeError, ValueError):
                pass
        return {
            "source": source,
            "interval_h": interval_h,
            "changes": [dict(r) for r in changes],
            "runs": runs_list,
            "last_run": runs_list[0] if runs_list else None,
            "field_labels": {
                "housing_class": "Класс жилья", "developer": "Застройщик",
                "district": "Район", "price_from": "Цена от", "price_m2": "Цена за м²",
                "area_min": "Площадь мин", "area_max": "Площадь макс",
                "rooms_min": "Комнат мин", "rooms_max": "Комнат макс",
                "stage_badge": "Стадия", "name": "Название",
            },
        }


    # Порядок и подписи вкладок hub-страницы /admin/parsers — "Общие данные"
    # первой (была самостоятельной страницей /admin/parser), дальше по
    # одной вкладке на каждый реальный парсер/скрейпер из _parser_registry_blocks().
    PARSERS_HUB_TABS = [
        {"key": "general", "label": "📊 Общие данные"},
        {"key": "krisha-apartments", "label": "🏠 Продажа"},
        {"key": "krisha-rental", "label": "🔑 Аренда"},
        {"key": "krisha-korter", "label": "🏢 Korter"},
        {"key": "krisha-homsters", "label": "🏗 Homsters"},
        {"key": "krisha-market", "label": "📊 Рынок"},
        {"key": "krisha-viewcount", "label": "👁 Просмотры"},
        {"key": "krisha-complex-scan", "label": "📸 ЖК (Крыша)"},
        {"key": "krisha-homeportal", "label": "🏛 Homeportal"},
        {"key": "novostroyki", "label": "🏗 Новостройки"},
        {"key": "recheck", "label": "🔁 Повторный обход"},
    ]

    async def _recheck_data(days: int):
        """Вкладка "Повторный обход" hub-страницы /admin/parsers (Task 2):
        покрытие recheck-обхода (возраст last_seen активных объявлений) +
        длительность полного круга глубокого обхода — раньше жило в
        неподключённом bot/templates/parser_detail.html (шаблон существовал,
        но ни один route его не рендерил) и вперемешку в "Общие данные";
        теперь отдельная вкладка, т.к. это один и тот же вопрос пользователя
        ("как у нас работает переобход уже известных объявлений и сколько
        он занимает")."""
        from bot.db.pg import fetch as pg_fetch, fetchval as pg_fetchval

        total_active = await pg_fetchval(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE") or 0
        buckets_row = await pg_fetch("""
            SELECT
              count(*) FILTER (WHERE last_seen > now() - interval '1 hour') AS b0,
              count(*) FILTER (WHERE last_seen <= now() - interval '1 hour' AND last_seen > now() - interval '6 hours') AS b1,
              count(*) FILTER (WHERE last_seen <= now() - interval '6 hours' AND last_seen > now() - interval '24 hours') AS b2,
              count(*) FILTER (WHERE last_seen <= now() - interval '24 hours' OR last_seen IS NULL) AS b3
            FROM apartment_listings WHERE is_active IS NOT FALSE
        """)
        b = buckets_row[0] if buckets_row else {}
        recheck_buckets = {
            "labels": ["< 1 ч", "1-6 ч", "6-24 ч", "> 24 ч"],
            "values": [b.get("b0", 0) or 0, b.get("b1", 0) or 0, b.get("b2", 0) or 0, b.get("b3", 0) or 0],
        }

        from bot.db import settings as app_settings
        await app_settings.load()
        full_cycle_sec = app_settings.get_int("DEEP_SWEEP_CIRCLE_DURATION_SEC", 0)
        full_cycle_completed_at = app_settings.get("DEEP_SWEEP_CIRCLE_COMPLETED_AT", "")
        full_cycle_hours = round(full_cycle_sec / 3600, 1) if full_cycle_sec else None
        deep_sweep_batch = app_settings.get_int("DEEP_SWEEP_BATCH", 5)

        # Market absorption — сколько объявлений уходит в архив по дням (тот
        # же концептуальный вопрос: как быстро мы "переобходим и списываем"
        # то, что уже есть в базе). 30 дней, независимо от фильтра days вкладки.
        absorption = await pg_fetch("""
            SELECT archived_at::date AS d, COUNT(*) AS cnt
            FROM apartment_listings
            WHERE archived_at > now() - interval '30 days'
            GROUP BY 1 ORDER BY 1
        """)

        return {
            "days": days,
            "total_active": total_active,
            "recheck_buckets": recheck_buckets,
            "full_cycle_hours": full_cycle_hours,
            "full_cycle_completed_at": full_cycle_completed_at,
            "deep_sweep_batch": deep_sweep_batch,
            "absorption_labels": [r["d"].strftime("%d.%m") for r in absorption],
            "absorption_values": [r["cnt"] for r in absorption],
        }

    async def _general_parser_stats(days: int):
        """Общая статистика продажи/аренды + графики — раньше вся страница
        /admin/parser (singular), теперь вкладка "Общие данные" hub-страницы
        /admin/parsers. Смотри также parser_sales_redirect/parser_rental_redirect
        ниже — старые URL всё ещё редиректят сюда."""
        from bot.db.pg import fetch as pg_fetch, fetchval as pg_fetchval

        sale_hourly = await pg_fetch("""
            SELECT date_trunc('hour', first_seen) AS h, COUNT(*) AS cnt
            FROM apartment_listings
            WHERE first_seen > now() - ($1 || ' days')::interval
            GROUP BY 1 ORDER BY 1
        """, str(days))
        rental_hourly = await pg_fetch("""
            SELECT date_trunc('hour', found_at) AS h, COUNT(*) AS cnt
            FROM rental_listings
            WHERE found_at > now() - ($1 || ' days')::interval
            GROUP BY 1 ORDER BY 1
        """, str(days))
        sale_by_h = {r["h"]: r["cnt"] for r in sale_hourly}
        rental_by_h = {r["h"]: r["cnt"] for r in rental_hourly}
        all_hours = sorted(set(sale_by_h) | set(rental_by_h))
        labels = [h.strftime("%d.%m %H:00") for h in all_hours]
        sale_values = [sale_by_h.get(h, 0) for h in all_hours]
        rental_values = [rental_by_h.get(h, 0) for h in all_hours]

        total_active = await pg_fetchval(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND COALESCE(is_duplicate, FALSE) = FALSE") or 0
        today_new_sale = await pg_fetchval(
            "SELECT COUNT(*) FROM apartment_listings WHERE first_seen::date = CURRENT_DATE") or 0
        today_archived = await pg_fetchval(
            "SELECT COUNT(*) FROM apartment_listings WHERE archived_at::date = CURRENT_DATE") or 0
        price_up = await pg_fetchval(
            "SELECT COUNT(DISTINCT listing_id) FROM price_history "
            "WHERE changed_at::date = CURRENT_DATE AND new_price > old_price") or 0
        price_down = await pg_fetchval(
            "SELECT COUNT(DISTINCT listing_id) FROM price_history "
            "WHERE changed_at::date = CURRENT_DATE AND new_price < old_price") or 0
        rental_total = await pg_fetchval("SELECT COUNT(*) FROM rental_listings") or 0
        rental_fresh = await pg_fetchval(
            "SELECT COUNT(*) FROM rental_listings WHERE last_seen > now() - interval '3 days'") or 0
        today_new_rental = await pg_fetchval(
            "SELECT COUNT(*) FROM rental_listings WHERE found_at::date = CURRENT_DATE") or 0

        from bot.db import settings as app_settings
        await app_settings.load()

        viewcount_total = await pg_fetchval(
            "SELECT COUNT(*) FROM apartment_listings WHERE views_count IS NOT NULL") or 0
        viewcount_fresh = await pg_fetchval(
            "SELECT COUNT(*) FROM apartment_listings WHERE views_count_updated_at > now() - interval '6 hours'") or 0
        viewcount_last_at = await pg_fetchval(
            "SELECT MAX(views_count_updated_at) FROM apartment_listings")

        top10_recalc_at = app_settings.get("DEAL_SCORE_LAST_RUN_AT", "")

        # "Полный обход Крыши: последний круг" переехал на отдельную вкладку
        # "🔁 Повторный обход" (Task 2) — не дублируем тут, это тот же вопрос
        # "как у нас работает переобход уже известных объявлений".
        stats = [
            {"label": "продажа: активных в мониторинге", "value": f"{total_active:,}".replace(",", " ")},
            {"label": "продажа: спаршено сегодня", "value": today_new_sale},
            {"label": "продажа: ушло в архив сегодня", "value": today_archived, "color": "#f59e0b"},
            {"label": "продажа: цена ↓ сегодня", "value": price_down, "color": "#16a34a"},
            {"label": "продажа: цена ↑ сегодня", "value": price_up, "color": "#ef4444"},
            {"label": "аренда: живых (видели за 3 дня)", "value": f"{rental_fresh:,}".replace(",", " ")},
            {"label": "аренда: всего в базе", "value": f"{rental_total:,}".replace(",", " ")},
            {"label": "аренда: спаршено сегодня", "value": today_new_rental},
            {"label": "просмотры: покрыто объявлений", "value": f"{viewcount_total:,}".replace(",", " ")},
            {"label": "просмотры: обновлено за 6 ч", "value": f"{viewcount_fresh:,}".replace(",", " ")},
        ]
        return {
            "days": days, "stats": stats,
            "chart_labels": labels,
            "sale_values": sale_values, "rental_values": rental_values,
            "viewcount_total": viewcount_total,
            "viewcount_fresh": viewcount_fresh,
            "viewcount_last_at": viewcount_last_at.strftime("%d.%m %H:%M") if viewcount_last_at else None,
            "top10_recalc_at": top10_recalc_at,
        }

    async def _homeportal_data():
        """Раньше отдельная страница /admin/analytics/homeportal, теперь
        вкладка "Homeportal" hub-страницы /admin/parsers."""
        from bot.db.pg import fetch as pg_fetch

        def one(rows):
            return rows[0] if rows else {}

        stats = {
            "total": one(await pg_fetch("SELECT count(*)::int AS n FROM homeportal_objects")).get("n", 0),
            "fetched": one(await pg_fetch("SELECT count(*)::int AS n FROM homeportal_objects WHERE fetched_at IS NOT NULL")).get("n", 0),
            "matched": one(await pg_fetch("SELECT count(*)::int AS n FROM homeportal_objects WHERE matched_complex_id IS NOT NULL")).get("n", 0),
            "unmatched": one(await pg_fetch("SELECT count(*)::int AS n FROM homeportal_objects WHERE matched_complex_id IS NULL")).get("n", 0),
            "errors": one(await pg_fetch("SELECT count(*)::int AS n FROM homeportal_parse_log WHERE status='error'")).get("n", 0),
        }
        recent = await pg_fetch("""SELECT h.fetched_at::timestamp(0) AS ts, h.name, h.rooms_1, h.rooms_2, h.rooms_3, h.rooms_4,
                                   h.apartments_total, h.developer_name, h.matched_complex_id, c.name AS cx_name
                                   FROM homeportal_objects h LEFT JOIN complexes c ON c.id = h.matched_complex_id
                                   ORDER BY h.fetched_at DESC NULLS LAST LIMIT 15""")
        hours = await pg_fetch("""SELECT to_char(date_trunc('hour', fetched_at), 'DD HH24:00') AS h,
                                   count(*)::int AS cnt FROM homeportal_objects
                                   WHERE fetched_at > now() - interval '24 hours'
                                   GROUP BY 1 ORDER BY 1""")
        return {
            "stats": stats, "recent": recent,
            "chart": {"hours": [h["h"] for h in hours], "cnt": [h["cnt"] for h in hours]},
        }

    async def _parse_monitor_data():
        """Раньше отдельная страница /admin/analytics/parse-monitor, теперь
        вкладка "ЖК (Крыша)" hub-страницы /admin/parsers."""
        from bot.db.pg import fetch as pg_fetch

        def one(rows):
            return rows[0] if rows else {}

        stats = {
            "total": one(await pg_fetch("SELECT count(*)::int AS n FROM complexes WHERE krisha_url IS NOT NULL")).get("n", 0),
            "filled": one(await pg_fetch("""SELECT count(*)::int AS n FROM housing_class_test hct
                             JOIN complexes c ON c.id = hct.complex_id
                             WHERE c.krisha_url IS NOT NULL AND hct.apartment_count_source = 'krisha'""")).get("n", 0),
            "pending": one(await pg_fetch("""SELECT count(*)::int AS n FROM complexes c
                              LEFT JOIN housing_class_test hct ON hct.complex_id = c.id
                              WHERE c.krisha_url IS NOT NULL
                                AND (hct.apartment_count_source IS DISTINCT FROM 'krisha')""")).get("n", 0),
            "errors": one(await pg_fetch("""SELECT count(*)::int AS n FROM krisha_parse_log
                             WHERE status = 'error' AND ts > now() - interval '24 hours'""")).get("n", 0),
        }
        set_rows = await pg_fetch("SELECT key, value FROM parse_settings")
        settings = {"delay": "120", "batch": "10", "enabled": "1"}
        for r in set_rows:
            settings[r["key"].replace("krisha_", "")] = r["value"]
        log = await pg_fetch("""SELECT l.ts::timestamp(0) AS ts, c.name, l.apartment_count, l.status, l.detail
                                      FROM krisha_parse_log l LEFT JOIN complexes c ON c.id = l.complex_id
                                      ORDER BY l.id DESC LIMIT 20""")
        hours = await pg_fetch("""SELECT to_char(date_trunc('hour', ts), 'DD HH24:00') AS h,
                                        count(*) FILTER (WHERE status='ok')::int AS ok,
                                        count(*) FILTER (WHERE status='error')::int AS err
                                        FROM krisha_parse_log WHERE ts > now() - interval '24 hours'
                                        GROUP BY 1 ORDER BY 1""")
        return {
            "stats": stats, "settings": settings, "log": log,
            "chart": {"hours": [h["h"] for h in hours],
                      "ok": [h["ok"] for h in hours],
                      "err": [h["err"] for h in hours]},
        }

    async def _novostroyki_data(developer_id: int = 0, days: int = 7) -> dict:
        """Данные вкладки "Новостройки" hub-страницы /admin/parsers: разбивка
        по застройщикам + по ЖК (для контроля что реально спарсилось) + график
        "спарсено во времени" (новых юнитов / ушло в продажу), с опциональным
        фильтром по одному застройщику — developer_id=0 значит "все"."""
        from bot.db.pg import fetch as pg_fetch

        all_developers = await pg_fetch("""
            SELECT DISTINCT d.id, d.name FROM complexes c
            JOIN developers d ON d.id = c.developer_id
            WHERE c.is_newbuild ORDER BY d.name
        """)
        by_developer = await pg_fetch("""
            SELECT d.id, d.name, count(DISTINCT c.id)::int AS complexes,
                   count(*) FILTER (WHERE u.status IN ('available','reserved'))::int AS active,
                   count(*) FILTER (WHERE u.status = 'sold')::int AS sold,
                   max(c.newbuild_last_scan_at) AS last_scan
            FROM complexes c
            JOIN developers d ON d.id = c.developer_id
            JOIN newbuild_units u ON u.complex_id = c.id
            WHERE c.is_newbuild
            GROUP BY d.id, d.name ORDER BY complexes DESC
        """)
        by_complex = await pg_fetch("""
            SELECT c.id, c.name, d.name AS developer, c.completion_year, c.completion_quarter,
                   c.newbuild_units_count, c.newbuild_sold_count, c.newbuild_last_scan_at
            FROM complexes c LEFT JOIN developers d ON d.id = c.developer_id
            WHERE c.is_newbuild AND ($1::int = 0 OR c.developer_id = $1)
            ORDER BY c.newbuild_units_count DESC NULLS LAST LIMIT 100
        """, developer_id)

        # График "спарсено во времени": новых юнитов (first_seen_at) и ушедших
        # в продажу (sold_at) по бакетам — два разных timestamp-столбца одной
        # таблицы, поэтому не подходит общий _activity_over_time (он на один
        # столбец), считаем отдельным запросом с UNION ALL.
        bucket = "hour" if days <= 3 else "day"
        fmt = "%d.%m %H:00" if bucket == "hour" else "%d.%m"
        rows = await pg_fetch(f"""
            SELECT date_trunc('{bucket}', ts) AS b, kind, COUNT(*) AS cnt FROM (
                SELECT first_seen_at AS ts, 'new' AS kind FROM newbuild_units
                WHERE first_seen_at > now() - ($2 || ' days')::interval
                  AND ($1::int = 0 OR developer_id = $1)
                UNION ALL
                SELECT sold_at AS ts, 'sold' AS kind FROM newbuild_units
                WHERE sold_at IS NOT NULL AND sold_at > now() - ($2 || ' days')::interval
                  AND ($1::int = 0 OR developer_id = $1)
            ) x GROUP BY 1, 2 ORDER BY 1
        """, developer_id, str(days))
        buckets = sorted({r["b"] for r in rows})
        by_bucket_kind = {(r["b"], r["kind"]): r["cnt"] for r in rows}
        chart_nb = {
            "bucket": bucket,
            "labels": [b.strftime(fmt) for b in buckets],
            "new": [by_bucket_kind.get((b, "new"), 0) for b in buckets],
            "sold": [by_bucket_kind.get((b, "sold"), 0) for b in buckets],
        }

        return {"by_developer": by_developer, "by_complex": by_complex,
                "all_developers": all_developers, "chart_nb": chart_nb}

    @router.post("/admin/parsers/novostroyki/run")
    async def novostroyki_run_now(request: Request):
        """Ручной запуск полного обхода новостроек (пока только BI Group —
        см. bi_group_import.py) прямо из админки, не дожидаясь cron. Не
        ждём завершения (весь каталог Астаны ~3-4 минуты) — фронт покажет
        "запущено", свежие цифры появятся на странице при следующем заходе."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        import asyncio as _aio
        import sys as _sys
        project_root = os.path.dirname(os.path.abspath(__file__))
        await _aio.create_subprocess_exec(
            _sys.executable, os.path.join(project_root, "bi_group_import.py"),
            cwd=project_root,
            stdout=_aio.subprocess.DEVNULL, stderr=_aio.subprocess.DEVNULL,
        )
        logger.info("novostroyki: ручной запуск bi_group_import.py")
        return JSONResponse({"ok": True, "started": True})

    @router.get("/admin/parsers", response_class=HTMLResponse)
    async def parsers_page(request: Request, tab: str = "general", days: int = 1, developer: int = 0):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        blocks = await _parser_registry_blocks()
        valid_keys = {t["key"] for t in PARSERS_HUB_TABS}
        if tab not in valid_keys:
            tab = "general"
        active_block = next((b for b in blocks if b["key"] == tab), None)

        ctx = {
            "request": request, "atab": "parsers",
            "tabs": PARSERS_HUB_TABS, "tab": tab,
            "blocks": blocks, "active_block": active_block,
        }
        if tab == "general":
            days = days if days in (1, 3, 5) else 1
            ctx.update(await _general_parser_stats(days))
        elif tab == "recheck":
            days = days if days in (1, 3, 7, 30) else 7
            ctx.update(await _recheck_data(days))
        elif tab == "krisha-homeportal":
            ctx.update(await _homeportal_data())
        elif tab == "krisha-complex-scan":
            ctx.update(await _parse_monitor_data())
        elif tab == "novostroyki":
            days = days if days in (1, 3, 7, 30) else 7
            ctx["nb_days"] = days
            ctx["nb_developer"] = developer
            ctx.update(await _novostroyki_data(developer, days))
        elif tab in ("krisha-korter", "krisha-homsters"):
            days = days if days in (1, 3, 7, 30) else 7
            src = "korter" if tab == "krisha-korter" else "homsters"
            ctx["days"] = days
            ctx["source_label"] = "Korter.kz" if src == "korter" else "Homsters.kz"
            ctx.update(await _source_changes_data(src, days))

        # Task 1: график "что и когда спарсилось" — на каждой вкладке
        # реального парсера (все, кроме general/recheck, у которых свои
        # графики уже есть выше). Один и тот же helper для всех 8, разница
        # только в table/ts_col/фильтре (PARSER_ACTIVITY_SPEC).
        if tab in PARSER_ACTIVITY_SPEC:
            adays = days if days in (1, 3, 7, 30) else 7
            table, ts_col, extra_where, title = PARSER_ACTIVITY_SPEC[tab]
            ctx["activity_days"] = adays
            ctx["activity_title"] = title
            ctx["chart_activity"] = await _activity_over_time(table, ts_col, adays, extra_where)

        return templates.TemplateResponse("parsers.html", ctx)

    @router.post("/admin/parsers/toggle/{key}")
    async def parsers_toggle(request: Request, key: str):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)

        if key == "krisha-complex-scan":
            await app_settings.load()
            new_value = "0" if app_settings.get_bool("PARSER_KRISHA_COMPLEX_SCAN", True) else "1"
            await app_settings.set("PARSER_KRISHA_COMPLEX_SCAN", new_value)
            logger.info("PARSER_KRISHA_COMPLEX_SCAN -> %s", new_value)
            return JSONResponse({"ok": True, "active": new_value == "1"})

        if key == "novostroyki":
            await app_settings.load()
            new_value = "0" if app_settings.get_bool("PARSER_NEWBUILD_SCAN", True) else "1"
            await app_settings.set("PARSER_NEWBUILD_SCAN", new_value)
            logger.info("PARSER_NEWBUILD_SCAN -> %s", new_value)
            return JSONResponse({"ok": True, "active": new_value == "1"})

        if key not in PARSERS_SYSTEMD:
            return JSONResponse({"error": "unknown parser"}, status_code=404)

        active = await _is_active(key)
        # Просто stop/start (НЕ disable/enable) — пауза процесса, но systemd
        # всё равно поднимет его после перезагрузки сервера (это и нужно).
        action = "stop" if active else "start"
        ok, msg = await _systemctl(action, key)
        new_active = await _is_active(key)
        logger.info("parser %s -> %s (%s)", key, action, "ok" if ok else msg)
        return JSONResponse({"ok": ok, "active": new_active, "msg": msg})

    # ── Топ-10 по скору ──────────────────────────────────────────────────

    @router.get("/admin/top10", response_class=HTMLResponse)
    async def top10_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        rows = await fetch("""
            SELECT id, url, title, rooms, district, complex_name, area, floor,
                   floors_total, price, est_rent, yield_pct, score_total,
                   COALESCE(zone_bonus, 0) AS zone_bonus, zone_name,
                   COALESCE(layer_bonus, 0) AS layer_bonus, layer_details, market_type,
                   COALESCE(price_drop_bonus, 0) AS price_drop_bonus,
                   bargain_rec, bargain_target, is_owner, year_built, last_seen
            FROM apartment_listings
            WHERE score_total IS NOT NULL
              AND COALESCE(is_duplicate, FALSE) = FALSE
              AND is_active IS NOT FALSE
              AND last_seen > now() - interval '14 days'
              AND price >= 500000
              AND COALESCE(yield_pct, 0) <= 100
            ORDER BY (score_total + COALESCE(zone_bonus, 0) + COALESCE(layer_bonus, 0) + COALESCE(price_drop_bonus, 0)) DESC,
                     yield_pct DESC NULLS LAST
            LIMIT 10
        """)
        return templates.TemplateResponse("top10.html", {
            "request": request,
            "listings": [dict(r) for r in rows],
        })

    # ── Инфо-страница: объяснения метрик ─────────────────────────────────

    @router.get("/admin/info", response_class=HTMLResponse)
    async def info_page(request: Request):
        # Публичная страница ("Инфо" в верхнем паб-нав) — объяснения метрик
        # для любого посетителя, не только админа. Раньше редиректила на
        # логин, хотя ссылка на неё есть в публичном меню — anonymous-визит
        # по клику из шапки сайта вёл на страницу входа вместо контента.
        await app_settings.load()
        # «Путь объявления» — живые цифры из БД (блок в info.html)
        from bot.db.pg import fetchval as pg_fval
        lifecycle_active_total = await pg_fval(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE") or 0
        lifecycle_recheck_1h = await pg_fval(
            "SELECT COUNT(*) FROM apartment_listings WHERE last_seen > now() - interval '1 hour'") or 0
        lifecycle_recheck_24h = await pg_fval(
            "SELECT COUNT(*) FROM apartment_listings WHERE last_seen > now() - interval '24 hours'") or 0
        lifecycle_price_changes_7d = 0
        try:
            lifecycle_price_changes_7d = await pg_fval(
                "SELECT COUNT(*) FROM price_history WHERE changed_at > now() - interval '7 days'") or 0
        except Exception:
            pass
        lifecycle_archived_7d = await pg_fval(
            "SELECT COUNT(*) FROM apartment_listings WHERE archived_at > now() - interval '7 days'") or 0
        lifecycle_recheck_1h_pct = round(100.0 * lifecycle_recheck_1h / lifecycle_active_total, 1) if lifecycle_active_total else 0
        lifecycle_recheck_24h_pct = round(100.0 * lifecycle_recheck_24h / lifecycle_active_total, 1) if lifecycle_active_total else 0
        deep_sweep_batch = app_settings.get_int("DEEP_SWEEP_BATCH", 5)
        detail_fetch_batch = app_settings.get_int("DETAIL_FETCH_BATCH", 30)
        # Длительность полного круга deep-sweep (если завершался): от последнего
        # завершения до предыдущего маркера — берём упрощённо: если завершался,
        # показываем '>24ч' как оценку; точных данных старта круга нет.
        deep_sweep_circle_hours = None
        if app_settings.get("DEEP_SWEEP_CIRCLE_COMPLETED_AT", None):
            deep_sweep_circle_hours = ">24"
        return templates.TemplateResponse("info.html", {
            "request": request,
            "deposit_rate": app_settings.get_float("DEPOSIT_RATE", 14.0),
            "appreciation": app_settings.get_float("APPRECIATION_PCT", 8.0),
            "max_pages": app_settings.get_int("PARSER_MAX_PAGES", 5),
            # Рыночные данные (справочные, собирает service_market_data раз в ~7 дней)
            "nbrk_rate": app_settings.get("NBRK_BASE_RATE", None),
            "kdif_rates": (app_settings.get("KDIF_RATES_RAW", "") or "").split(" ;; "),
            "otbasy_rates": (app_settings.get("OTBASY_RATES_RAW", "") or "").split(" ;; "),
            "stat_rows": (app_settings.get("STAT_HOUSING_ASTANA_RAW", "") or "").split("\n"),
            "stat_url": app_settings.get("STAT_HOUSING_FILE_URL", None),
            "market_updated": app_settings.get("MARKET_DATA_UPDATED_AT", None),
            # «Путь объявления»
            "lifecycle_active_total": lifecycle_active_total,
            "lifecycle_recheck_1h": lifecycle_recheck_1h,
            "lifecycle_recheck_1h_pct": lifecycle_recheck_1h_pct,
            "lifecycle_recheck_24h_pct": lifecycle_recheck_24h_pct,
            "lifecycle_price_changes_7d": lifecycle_price_changes_7d,
            "lifecycle_archived_7d": lifecycle_archived_7d,
            "deep_sweep_batch": deep_sweep_batch,
            "deep_sweep_circle_hours": deep_sweep_circle_hours,
            "detail_fetch_batch": detail_fetch_batch,
        })

    @router.get("/admin/admin-info", response_class=HTMLResponse)
    async def admin_info_page(request: Request):
        """ИНФО для админа: пометка старого фонда (пятиэтажки до 700 тыс/м²,
        год 1970) + тепловая карта новизны домов Астаны."""
        if not is_authed(request):
            return RedirectResponse("/admin/login")
        from bot.db.pg import fetchval as pg_fval, fetch as pg_fetch
        old_fund_total = await pg_fval("SELECT COUNT(*) FROM house_years WHERE is_old_fund") or 0
        old_fund_1970 = await pg_fval("SELECT COUNT(*) FROM house_years WHERE is_old_fund AND year_built = 1970") or 0
        hy_total = await pg_fval("SELECT COUNT(*) FROM house_years") or 0
        hy_with_year = await pg_fval("SELECT COUNT(*) FROM house_years WHERE year_built IS NOT NULL") or 0
        # распределение по годам (десятилетия) для сводки
        decade_rows = await pg_fetch("""
            SELECT (year_built / 10 * 10) AS dec, COUNT(*)
            FROM house_years WHERE year_built IS NOT NULL
            GROUP BY 1 ORDER BY 1
        """)
        decades = [{"dec": r["dec"], "cnt": r["count"]} for r in decade_rows]
        return templates.TemplateResponse("admin_info.html", {
            "request": request,
            "old_fund_total": old_fund_total,
            "old_fund_1970": old_fund_1970,
            "hy_total": hy_total,
            "hy_with_year": hy_with_year,
            "decades": decades,
        })

    @router.get("/admin/api/novelty-points")
    async def novelty_points_api(request: Request):
        """Точки домов с годом постройки (house_years JOIN объявлений) —
        для тепловой карты новизны на главной (режим 'novelty')."""
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT hy.address, hy.year_built, hy.is_old_fund,
                   AVG(a.lat) AS lat, AVG(a.lon) AS lon, COUNT(*) AS cnt
            FROM house_years hy
            JOIN apartment_listings a
              ON lower(trim(regexp_replace(a.address, '\\s*—.*$', ''))) = hy.address
            WHERE a.lat IS NOT NULL AND a.lon IS NOT NULL
              AND a.is_active IS NOT FALSE AND COALESCE(a.is_duplicate, FALSE) = FALSE
            GROUP BY hy.address, hy.year_built, hy.is_old_fund
            ORDER BY hy.year_built NULLS LAST
        """)
        pts = [{
            "y": r["year_built"], "old": bool(r["is_old_fund"]),
            "lat": float(r["lat"]), "lon": float(r["lon"]),
        } for r in rows if r["lat"] is not None]
        return JSONResponse({"points": pts, "count": len(pts)})

    @router.get("/admin/api/admin-info-heat")
    async def admin_info_heat_api(request: Request):
        """Точки домов (lat/lon + year) для тепловой карты новизны:
        house_years JOIN с объявлениями по нормализованному адресу."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT hy.address, hy.year_built, hy.is_old_fund,
                   AVG(a.lat) AS lat, AVG(a.lon) AS lon,
                   COUNT(*) AS cnt
            FROM house_years hy
            JOIN apartment_listings a
              ON lower(trim(regexp_replace(a.address, '\\s*—.*$', ''))) = hy.address
            WHERE a.lat IS NOT NULL AND a.lon IS NOT NULL
              AND a.is_active IS NOT FALSE AND COALESCE(a.is_duplicate, FALSE) = FALSE
            GROUP BY hy.address, hy.year_built, hy.is_old_fund
            ORDER BY hy.year_built NULLS LAST
        """)
        pts = [{
            "address": r["address"], "year": r["year_built"],
            "old_fund": bool(r["is_old_fund"]), "cnt": r["cnt"],
            "lat": float(r["lat"]), "lon": float(r["lon"]),
        } for r in rows if r["lat"] is not None]
        return JSONResponse({"points": pts, "count": len(pts)})

    @router.get("/admin/investments", response_class=HTMLResponse)
    async def investments_page(request: Request):
        # Публичная, как /admin/info рядом (та же вкладочная группа
        # "Аналитика") — тепловая карта доходности (задача "Инвестиции").
        from bot.db.pg import fetchrow as pg_fr
        row = await pg_fr("""
            SELECT COUNT(*) AS n, AVG(yield_pct) AS avg_yield,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY yield_pct) AS median_yield
            FROM apartment_listings
            WHERE is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
              AND yield_pct IS NOT NULL AND lat IS NOT NULL
        """)
        return templates.TemplateResponse("investments.html", {
            "request": request,
            "n": row["n"] if row else 0,
            "avg_yield": round(row["avg_yield"], 1) if row and row["avg_yield"] else None,
            "median_yield": round(row["median_yield"], 1) if row and row["median_yield"] else None,
        })

    @router.get("/admin/krisha-lookup", response_class=HTMLResponse)
    async def krisha_lookup_page(request: Request):
        # Та же вкладочная группа "Аналитика", что /admin/info и
        # /admin/investments рядом. Раньше форма "Вставьте ссылку с Крыши"
        # жила прямо в строке фильтров на главной карте — переехала сюда
        # отдельной страницей (задача), сама главная карта её больше не
        # показывает. Логика поиска не дублируется: просто вырезаем id
        # объявления и редиректим на уже существующий /admin/listing/{id}
        # (та же карта с открытым большим попапом, см. admin_web.py).
        return templates.TemplateResponse("krisha_lookup.html", {"request": request})

    # ── API: точки для карты на дашборде ─────────────────────────────────

    # ── Детализация парсера: один график продажи+аренда, разными цветами ───

    # /admin/parser (singular) и его старые под-URL слиты во вкладку
    # "Общие данные" hub-страницы /admin/parsers — см. задачу "reorganize
    # into 4 tabbed hub pages". Оставляем редиректы, чтобы старые ссылки/
    # закладки не 404-или.
    @router.get("/admin/parser/sales")
    async def parser_sales_redirect(days: int = 1):
        return RedirectResponse(url=f"/admin/parsers?tab=general&days={days}", status_code=301)

    @router.get("/admin/parser/rental")
    async def parser_rental_redirect(days: int = 1):
        return RedirectResponse(url=f"/admin/parsers?tab=general&days={days}", status_code=301)

    @router.get("/admin/parser", response_class=HTMLResponse)
    async def parser_combined_redirect(days: int = 1):
        return RedirectResponse(url=f"/admin/parsers?tab=general&days={days}", status_code=301)

    @router.get("/admin/duplicates", response_class=HTMLResponse)
    async def duplicates_page(request: Request):
        """Страница дублей: кто чей дубль, со ссылками."""
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import fetch as pg_fetch
        # dup_match/dup_needs_review/dedup_scan_log — см. migrations/028_dup_columns.sql
        # (раньше эти ALTER TABLE гонялись тут на каждый заход на страницу —
        # колонки давно существуют, а лишний ALTER TABLE на живой активно
        # пишущейся таблице иногда не мог получить лок и падал по таймауту).
        rows = await pg_fetch("""
            SELECT p.id, p.address, p.price, p.rooms, p.area, p.is_owner,
                   p.seller_name, p.lat, p.lon, p.complex_name, p.floor, p.floors_total,
                   p.photos AS p_photos, COUNT(d.id) AS dup_cnt,
                   bool_or(COALESCE(d.dup_needs_review, FALSE)) AS needs_review,
                   json_agg(json_build_object(
                       'id', d.id, 'price', d.price, 'is_owner', d.is_owner,
                       'url', d.url, 'match', COALESCE(d.dup_match, '?'),
                       'seller_name', d.seller_name, 'floor', d.floor,
                       'floors_total', d.floors_total, 'photos', d.photos,
                       'rooms', d.rooms, 'area', d.area, 'needs_review', COALESCE(d.dup_needs_review, FALSE)
                       ) ORDER BY d.is_owner DESC NULLS LAST, d.price ASC) AS dups
            FROM apartment_listings p
            JOIN apartment_listings d ON d.duplicate_of = p.id AND d.is_duplicate = TRUE
            GROUP BY p.id
            ORDER BY needs_review DESC, dup_cnt DESC, p.last_seen DESC NULLS LAST
            LIMIT 300
        """)
        dup_timeline = await pg_fetch("""
            SELECT date_trunc('day', dup_marked_at)::date AS day, COUNT(*) AS n
            FROM apartment_listings
            WHERE is_duplicate = TRUE AND dup_marked_at IS NOT NULL
            GROUP BY 1 ORDER BY 1
        """)
        scan_timeline = await pg_fetch("""
            SELECT scanned_at, listings_scanned, duplicates_found, needs_review_found
            FROM dedup_scan_log WHERE table_name = 'apartment_listings'
            ORDER BY scanned_at DESC LIMIT 200
        """)
        similar = []  # блок «Похожие в ЖК» убран со страницы дублей по запросу
        rent_cnt = 0
        try:
            from bot.db.pg import fetchval as pg_fetchval
            rent_cnt = await pg_fetchval(
                "SELECT COUNT(*) FROM rental_listings WHERE is_duplicate = TRUE") or 0
        except Exception:
            pass
        import json as _json2
        def _photos_list(v):
            if isinstance(v, str):
                try:
                    v = _json2.loads(v)
                except ValueError:
                    v = []
            return (v or [])[:1]  # только первое фото нужно для мини-карточки
        out_rows = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("dups"), str):
                d["dups"] = _json2.loads(d["dups"])
            d["photo"] = (_photos_list(d.pop("p_photos", None)) or [None])[0]
            for dd in d["dups"]:
                dd["photo"] = (_photos_list(dd.pop("photos", None)) or [None])[0]
            out_rows.append(d)
        out_sim = []
        for r in similar:
            d = dict(r)
            if isinstance(d.get("items"), str):
                d["items"] = _json2.loads(d["items"])
            out_sim.append(d)
        return templates.TemplateResponse("duplicates.html", {
            "request": request,
            "atab": "dups",
            "rows": out_rows,
            "similar": out_sim,
            "rent_cnt": rent_cnt,
            "dup_timeline": [{"day": r["day"].strftime("%Y-%m-%d"), "n": r["n"]} for r in dup_timeline],
            "scan_timeline": [{
                "at": r["scanned_at"].strftime("%Y-%m-%d %H:%M"),
                "scanned": r["listings_scanned"], "found": r["duplicates_found"],
                "review": r["needs_review_found"],
            } for r in reversed(scan_timeline)],
        })

    @router.get("/admin/api/duplicates")
    async def api_duplicates(request: Request):
        """Статистика дублей для дашборда."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetchval as pg_fetchval
        apt = await pg_fetchval(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_duplicate = TRUE") or 0
        rent = 0
        try:
            rent = await pg_fetchval(
                "SELECT COUNT(*) FROM rental_listings WHERE is_duplicate = TRUE") or 0
        except Exception:
            pass  # колонка появится после первого прогона дедупа аренды
        return JSONResponse({"apartments": apt, "rentals": rent})

    @router.get("/admin/api/city-poi")
    async def city_poi(request: Request):
        """Школы/садики/вузы города — для тепловой карты школ и для отдельного
        слоя меток "🏫 Школы" на главном дашборде.
        БАГ (найден): эта ручка требовала админ-логин, хотя сам дашборд и обе
        кнопки ("Школы" на карте, "Школы" в тепловых картах) публичны — для
        не залогиненного посетителя они молча ничего не показывали (401 без
        видимой ошибки в UI). Публичная, как /admin/api/city-roads и
        /admin/api/map-points рядом — ничего чувствительного тут нет."""
        from bot.db.pg import fetch as pg_fetch
        # Раньше без WHERE отдавал вообще всю city_poi (школы+садики+вузы+
        # клиники+госорганы+ЛРТ-точки, см. poi_import.py) — кнопка "Школы и
        # садики" на дашборде должна показывать только школы и садики.
        rows = await pg_fetch(
            "SELECT kind, name, lat, lon, address FROM city_poi "
            "WHERE kind IN ('school', 'kindergarten') LIMIT 3000")
        return JSONResponse({"poi": [dict(r) for r in rows]})

    @router.get("/admin/complex/find")
    async def complex_find(request: Request, name: str = ""):
        """Переход на карточку ЖК по имени (для ссылок из попапов карты).
        Точное совпадение lower/trim; если ЖК нет — на список с поиском."""
        from bot.db.pg import fetchval as pg_fv
        cid = None
        if name.strip():
            cid = await pg_fv(
                "SELECT id FROM complexes WHERE lower(trim(name)) = lower(trim($1)) LIMIT 1",
                name)
        if cid:
            return RedirectResponse(url=f"/admin/complex/{cid}", status_code=302)
        from urllib.parse import quote
        return RedirectResponse(url=f"/admin/complexes?search={quote(name)}", status_code=302)

    @router.get("/admin/api/complexes-layer")
    async def complexes_layer(request: Request):
        """Публичный слой ЖК для главного дашборда (кружки-контуры + попап).
        Лёгкая версия complexes-map — без сортировки/coverage, только то,
        что нужно нарисовать на карте и показать в превью."""
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT c.id, c.name, c.year_built, c.housing_class,
                   COALESCE(c.listings_count, 0) AS active_cnt,
                   COALESCE(d.name,
                            c.source_info->'korter'->>'developer',
                            c.source_info->'homsters'->>'developer') AS developer,
                   c.avg_price_m2, c.photo_url,
                   c.lat AS c_lat, c.lon AS c_lon, g.lat, g.lon, g.avg_score
            FROM complexes c
            LEFT JOIN developers d ON d.id = c.developer_id
            LEFT JOIN LATERAL (
                SELECT AVG(al.lat) AS lat, AVG(al.lon) AS lon,
                       AVG(COALESCE(score_total,0) + COALESCE(zone_bonus,0)
                           + COALESCE(layer_bonus,0) + COALESCE(price_drop_bonus,0))
                         FILTER (WHERE is_active IS NOT FALSE) AS avg_score
                FROM apartment_listings al
                WHERE lower(trim(regexp_replace(al.complex_name, '^\\s*(жк|кг)\\.?\\s+', '', 'i')))
                      = lower(trim(regexp_replace(c.name, '^\\s*(жк|кг)\\.?\\s+', '', 'i')))
            ) g ON TRUE
            WHERE COALESCE(c.lat, g.lat) IS NOT NULL
              AND COALESCE(c.is_street, FALSE) = FALSE
            ORDER BY c.id
            LIMIT 5000
        """)
        return JSONResponse({"complexes": [{
            "id": r["id"], "name": r["name"], "year": r["year_built"],
            "class": r["housing_class"], "active": r["active_cnt"],
            "developer": r["developer"] or "",
            "price_m2": round(float(r["avg_price_m2"])) if r["avg_price_m2"] else None,
            "avg_score": round(float(r["avg_score"])) if r["avg_score"] else None,
            "photo": r["photo_url"],
            "lat": float(r["c_lat"] if r["c_lat"] is not None else r["lat"]),
            "lon": float(r["c_lon"] if r["c_lon"] is not None else r["lon"]),
        } for r in rows]})

    @router.get("/admin/api/complex-summary/{complex_id}")
    async def complex_summary(request: Request, complex_id: int):
        """Публичная сводка по ЖК для попапа на главном дашборде: фото,
        класс, средние цены, описание (residents_notes), список объявлений."""
        from bot.db.pg import fetchrow as pg_fetchrow, fetch as pg_fetch
        cx = await pg_fetchrow("""
            SELECT c.*, d.name AS developer_name
            FROM complexes c LEFT JOIN developers d ON d.id = c.developer_id
            WHERE c.id = $1
        """, complex_id)
        if not cx:
            return JSONResponse({"error": "not_found"}, status_code=404)
        developer = cx["developer_name"]
        developer_logo = None
        if cx.get("developer_id"):
            _dl = await fetchrow("SELECT logo FROM developers WHERE id = $1", cx["developer_id"])
            if _dl:
                developer_logo = _dl["logo"]
        if not developer and cx["source_info"]:
            si = cx["source_info"]
            if isinstance(si, str):
                import json as _j
                try:
                    si = _j.loads(si)
                except ValueError:
                    si = {}
            if isinstance(si, dict):
                developer = ((si.get("korter") or {}).get("developer")
                             or (si.get("homsters") or {}).get("developer"))
        listings = await pg_fetch("""
            SELECT id, price, rooms, area, url, photos
            FROM apartment_listings
            WHERE lower(trim(complex_name)) = lower(trim($1))
              AND is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
            ORDER BY score_total DESC NULLS LAST LIMIT 8
        """, cx["name"])
        import json as _json_cs

        def _first_photo(v):
            if isinstance(v, str):
                try:
                    v = _json_cs.loads(v)
                except ValueError:
                    v = []
            return (v or [None])[0]

        return JSONResponse({
            "id": cx["id"], "name": cx["name"],
            "photo": cx["photo_url"],
            "housing_class": cx["housing_class"],
            "developer": developer or "",
            "developer_id": cx["developer_id"],
            "year_built": cx["year_built"],
            "avg_price_m2": round(float(cx["avg_price_m2"])) if cx["avg_price_m2"] else None,
            "description": cx["residents_notes"] or "",
            "listings_count": cx["listings_count"] or 0,
            "listings": [{
                "id": l["id"], "price": l["price"], "rooms": l["rooms"],
                "area": float(l["area"]) if l["area"] else None,
                "url": l["url"], "photo": _first_photo(l["photos"]),
            } for l in listings],
        })

    # ── Новостройки: точки на карту, юниты ЖК, деталь юнита ────────────────
    # Публичные ручки (раздел "можно будет показывать всем") — та же логика
    # анонимного доступа, что у complexes_map ниже: без is_authed вообще.

    @router.get("/admin/api/newbuild-developers")
    async def newbuild_developers(request: Request):
        """Список застройщиков с живыми новостройками — для панели-фильтра
        слева на карте в режиме "Новостройки" (см. dashboard.html). Отдельная
        ручка, а не выборка из newbuild-map-points, чтобы список галочек не
        мигал при смене фильтра комнатности/года (стабилен между перерисовками)."""
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT DISTINCT d.id, d.name, d.logo
            FROM complexes c JOIN developers d ON d.id = c.developer_id
            WHERE c.is_newbuild AND c.lat IS NOT NULL AND c.lon IS NOT NULL
            ORDER BY d.name
        """)
        return JSONResponse({"developers": [
            {"id": r["id"], "name": r["name"], "logo": r["logo"]} for r in rows]})

    @router.get("/admin/api/newbuild-map-points")
    async def newbuild_map_points(request: Request, rooms: str = "", year: int = 0,
                                   developers: str = ""):
        """Маркеры ЖК-новостроек для режима "Новостройки" на главной карте.
        units_count — с учётом фильтра по комнатности (если задан) и года
        сдачи, пересчитывается на лету, а не берётся из кэша
        complexes.newbuild_units_count (тот — для всех комнатностей сразу,
        см. bi_group_import.save_realestate). developers — CSV id застройщиков
        из панели слева (см. newbuild_developers ниже), пусто = все."""
        from bot.db.pg import fetch as pg_fetch
        room_list = [int(r) for r in rooms.split(",") if r.strip().isdigit()]
        # НЕ .isdigit() — "-1".isdigit() == False в Python (минус не цифра),
        # а -1 нам как раз нужен: фронт шлёт его сентинелом "не выбрано ни
        # одного застройщика" (см. onDevCheckboxChange в dashboard.html),
        # который не должен совпасть ни с одним реальным id.
        dev_list = []
        for d in developers.split(","):
            d = d.strip()
            try:
                dev_list.append(int(d))
            except ValueError:
                pass
        rows = await pg_fetch("""
            SELECT c.id, c.name, c.lat, c.lon, c.completion_year, c.completion_quarter,
                   c.developer_id, d.name AS developer, d.logo, d.website AS developer_website,
                   c.housing_class, c.photos, c.source_info, c.address,
                   count(u.id) FILTER (
                       WHERE u.status IN ('available','reserved')
                         AND ($1::int[] = '{}' OR u.rooms = ANY($1::int[]))
                   ) AS units_count
            FROM complexes c
            JOIN developers d ON d.id = c.developer_id
            LEFT JOIN newbuild_units u ON u.complex_id = c.id
            WHERE c.is_newbuild AND c.lat IS NOT NULL AND c.lon IS NOT NULL
              AND ($2::int = 0 OR c.completion_year = $2)
              AND ($3::int[] = '{}' OR c.developer_id = ANY($3::int[]))
            GROUP BY c.id, c.name, c.lat, c.lon, c.completion_year, c.completion_quarter,
                     c.developer_id, d.name, d.logo, d.website, c.housing_class, c.photos, c.source_info
        """, room_list, year, dev_list)
        import json as _json_nbp
        pts = []
        for r in rows:
            ph = r.get("photos")
            if isinstance(ph, str):
                try:
                    ph = _json_nbp.loads(ph)
                except ValueError:
                    ph = None
            # Лендинг ЖК у застройщика (bi.group/sensata/...), если сохранили;
            # иначе — страница застройщика.
            si = r.get("source_info") or {}
            if isinstance(si, str):
                try:
                    si = _json_nbp.loads(si)
                except ValueError:
                    si = {}
            dev_url = si.get("bi_group_landing") or si.get("landing_url") or r.get("developer_website") or ""
            pts.append({
                "id": r["id"], "name": r["name"], "lat": float(r["lat"]), "lon": float(r["lon"]),
                "developer": r["developer"], "developer_id": r["developer_id"], "logo": r["logo"],
                "units_count": r["units_count"],
                "completion_year": r["completion_year"], "completion_quarter": r["completion_quarter"],
                "housing_class": r.get("housing_class") or "",
                "photos": (ph or [])[:5],
                "developer_url": dev_url,
                "address": r.get("address") or "",
            })
        return JSONResponse({"points": pts})

    @router.get("/admin/api/newbuild-complex/{complex_id}/units")
    async def newbuild_complex_units(request: Request, complex_id: int, rooms: str = ""):
        """Список вариантов квартир в наличии для попапа ЖК на карте (клик
        по маркеру) — как listings в complex_summary, только из newbuild_units."""
        from bot.db.pg import fetch as pg_fetch, fetchrow as pg_fetchrow
        import json as _json_nbcu
        cx = await pg_fetchrow("""
            SELECT c.id, c.name, c.completion_year, c.completion_quarter, c.district,
                   c.address, c.photos, d.id AS developer_id, d.name AS developer,
                   d.sales_phone, d.logo
            FROM complexes c JOIN developers d ON d.id = c.developer_id
            WHERE c.id = $1 AND c.is_newbuild
        """, complex_id)
        if not cx:
            return JSONResponse({"error": "not_found"}, status_code=404)
        room_list = [int(r) for r in rooms.split(",") if r.strip().isdigit()]
        units = await pg_fetch("""
            SELECT id, rooms, area, floor, floors_total, price, layout_photo_url, status
            FROM newbuild_units
            WHERE complex_id = $1 AND status IN ('available','reserved')
              AND ($2::int[] = '{}' OR rooms = ANY($2::int[]))
            ORDER BY price ASC NULLS LAST LIMIT 30
        """, complex_id, room_list)
        # photos — jsonb, asyncpg отдаёт строкой (см. тот же паттерн в complex_detail)
        cx_photos = cx["photos"]
        if isinstance(cx_photos, str):
            try:
                cx_photos = _json_nbcu.loads(cx_photos)
            except ValueError:
                cx_photos = None
        return JSONResponse({
            "id": cx["id"], "name": cx["name"], "district": cx["district"],
            "address": cx["address"], "photos": cx_photos or [],
            "completion_year": cx["completion_year"], "completion_quarter": cx["completion_quarter"],
            "developer": cx["developer"], "developer_id": cx["developer_id"], "sales_phone": cx["sales_phone"],
            "developer_logo": cx["logo"],
            "units": [{
                "id": u["id"], "rooms": u["rooms"],
                "area": float(u["area"]) if u["area"] is not None else None,
                "floor": u["floor"], "floors_total": u["floors_total"], "price": u["price"],
                "photo": u["layout_photo_url"], "status": u["status"],
            } for u in units],
        })

    @router.get("/admin/api/newbuild-unit/{unit_id}")
    async def newbuild_unit_detail(request: Request, unit_id: int):
        """Деталь одного варианта — для большого попапа (аналог
        /admin/api/listing/{id} у вторички, см. openDetailModal в
        dashboard.html: ветка по префиксу id 'nb-')."""
        from bot.db.pg import fetchrow as pg_fetchrow
        import json as _json_nbu
        u = await pg_fetchrow("""
            SELECT u.*, c.name AS complex_name, c.district, c.address,
                   c.description AS complex_description, c.photos AS complex_photos,
                   c.completion_year, c.completion_quarter, c.id AS complex_id,
                   d.id AS developer_id, d.name AS developer_name, d.sales_phone, d.logo
            FROM newbuild_units u
            JOIN complexes c ON c.id = u.complex_id
            JOIN developers d ON d.id = c.developer_id
            WHERE u.id = $1
        """, unit_id)
        if not u:
            return JSONResponse({"error": "not_found"}, status_code=404)
        cx_photos = u["complex_photos"]
        if isinstance(cx_photos, str):
            try:
                cx_photos = _json_nbu.loads(cx_photos)
            except ValueError:
                cx_photos = None
        return JSONResponse({
            "id": u["id"], "rooms": u["rooms"],
            "area": float(u["area"]) if u["area"] is not None else None,
            "floor": u["floor"], "floors_total": u["floors_total"],
            "building": u["building"], "section": u["section"],
            "price": u["price"],
            "price_per_m2": float(u["price_per_m2"]) if u["price_per_m2"] is not None else None,
            "photo": u["layout_photo_url"], "status": u["status"],
            "complex_id": u["complex_id"], "complex_name": u["complex_name"],
            "complex_description": u["complex_description"], "complex_photos": cx_photos or [],
            "district": u["district"], "address": u["address"],
            "completion_year": u["completion_year"], "completion_quarter": u["completion_quarter"],
            "developer_id": u["developer_id"], "developer_name": u["developer_name"],
            "developer_logo": u["logo"], "sales_phone": u["sales_phone"],
        })

    @router.get("/admin/api/complexes-map")
    async def complexes_map(request: Request):
        """Все ЖК с координатами (центроид объявлений) для карты рейтинга.
        /admin/complexes сама по себе публичная (см. complexes_page) — эта
        ручка кормит карту на ней, раньше 401'ила анонимам, из-за чего карта
        оставалась пустой для любого незалогиненного посетителя."""
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT c.id, c.name, c.year_built, c.housing_class,
                   COALESCE(c.listings_count, 0) AS active_cnt,
                   COALESCE(c.sold_count, 0) AS sold_cnt,
                   c.developer_id,
                   COALESCE(d.name,
                            c.source_info->'korter'->>'developer',
                            c.source_info->'homsters'->>'developer') AS developer,
                   c.avg_price_m2, c.lat AS c_lat, c.lon AS c_lon,
                   c.photo_url,
                   g.lat, g.lon, g.avg_score, g.avg_days_to_sell, g.sold_30d
            FROM complexes c
            LEFT JOIN developers d ON d.id = c.developer_id
            LEFT JOIN LATERAL (
                SELECT AVG(al.lat) AS lat, AVG(al.lon) AS lon,
                       AVG(COALESCE(score_total,0) + COALESCE(zone_bonus,0)
                           + COALESCE(layer_bonus,0) + COALESCE(price_drop_bonus,0))
                         FILTER (WHERE is_active IS NOT FALSE) AS avg_score,
                       AVG(EXTRACT(EPOCH FROM (al.archived_at - al.first_seen))/86400)
                         FILTER (WHERE al.archived_at IS NOT NULL) AS avg_days_to_sell,
                       COUNT(*) FILTER (WHERE al.archived_at >= now() - interval '30 days')
                         AS sold_30d
                FROM apartment_listings al
                WHERE lower(trim(regexp_replace(al.complex_name, '^\\s*(жк|кг)\\.?\\s+', '', 'i')))
                      = lower(trim(regexp_replace(c.name, '^\\s*(жк|кг)\\.?\\s+', '', 'i')))
            ) g ON TRUE
            WHERE COALESCE(c.lat, g.lat) IS NOT NULL
              AND COALESCE(c.is_street, FALSE) = FALSE
            ORDER BY c.id
            LIMIT 5000
        """)
        return JSONResponse({"complexes": [{
            "id": r["id"], "name": r["name"],
            "year": r["year_built"], "class": r["housing_class"],
            "active": r["active_cnt"], "sold": r["sold_cnt"],
            "developer": r["developer"] or "—",
            "developer_id": r["developer_id"],
            "avg_score": round(float(r["avg_score"])) if r["avg_score"] else None,
            "price_m2": round(float(r["avg_price_m2"])) if r["avg_price_m2"] else None,
            "days_to_sell": round(float(r["avg_days_to_sell"])) if r["avg_days_to_sell"] else None,
            "sold_30d": r["sold_30d"] or 0,
            "photo": r["photo_url"],
            "lat": float(r["c_lat"] if r["c_lat"] is not None else r["lat"]),
            "lon": float(r["c_lon"] if r["c_lon"] is not None else r["lon"]),
        } for r in rows]})

    @router.get("/admin/api/deep-sweep-status")
    async def deep_sweep_status(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        await app_settings.load()
        return JSONResponse({
            "cursor": app_settings.get_int("DEEP_SWEEP_PAGE", 0),
            "batch": app_settings.get_int("DEEP_SWEEP_BATCH", 5),
            "last_at": app_settings.get("DEEP_SWEEP_LAST_AT", None),
        })

    @router.get("/admin/api/map-points-lite")
    async def map_points_lite(request: Request, type: str = "sale", rooms: str = "",
                              price_min: float = 0, price_max: float = 0,
                              area_min: float = 0, area_max: float = 0,
                              seller: str = "", market: str = "",
                              has_floorplan: bool = False):
        """Только id/lat/lon, без джойнов и фото — для отдалённого вида карты
        (zoom < ZOOM_GATE в dashboard.html), где нужно просто показать, ГДЕ
        есть объявления (кластерами-кружками с числом), а не тянуть полные
        карточки, которые пока никто не увидит по отдельности."""
        from bot.db.pg import fetch as pg_fetch

        if type == "rental":
            rows = await pg_fetch("""
                SELECT lat, lon FROM rental_listings
                WHERE lat IS NOT NULL AND lon IS NOT NULL
                  AND last_seen > now() - interval '14 days'
                  AND COALESCE(is_duplicate, FALSE) = FALSE
                LIMIT 20000
            """)
            return JSONResponse({"points": [[float(r["lat"]), float(r["lon"])] for r in rows]})

        conds, params, i = [], [], 1
        if rooms:
            # UI отдаёт список через запятую при выборе нескольких чекбоксов
            # комнатности ("1,2") — int(rooms) на такой строке падал с
            # ValueError, и весь запрос (а с ним и вся карта на этом зуме) молча
            # переставал отвечать. rooms = ANY(...) работает и для одного, и для
            # нескольких значений.
            room_list = [int(x) for x in rooms.split(',') if x.strip().isdigit()]
            if room_list:
                conds.append(f"AND rooms = ANY(${i})"); params.append(room_list); i += 1
        if price_min > 0:
            conds.append(f"AND price >= ${i}"); params.append(int(price_min)); i += 1
        if price_max > 0:
            conds.append(f"AND price <= ${i}"); params.append(int(price_max)); i += 1
        if area_min > 0:
            conds.append(f"AND area >= ${i}"); params.append(area_min); i += 1
        if area_max > 0:
            conds.append(f"AND area <= ${i}"); params.append(area_max); i += 1
        if seller == "owner":
            conds.append("AND is_owner IS TRUE")
        elif seller == "agent":
            conds.append("AND is_owner IS DISTINCT FROM TRUE")
        if market == "primary":
            conds.append("AND market_type = 'primary'")
        elif market == "secondary":
            conds.append("AND COALESCE(market_type, 'secondary') <> 'primary'")
        if has_floorplan:
            conds.append("AND floorplan_url IS NOT NULL")

        rows = await pg_fetch(f"""
            SELECT lat, lon FROM apartment_listings
            WHERE lat IS NOT NULL AND lon IS NOT NULL
              AND is_active IS NOT FALSE
              AND COALESCE(is_duplicate, FALSE) = FALSE
              AND last_seen > now() - interval '14 days'
              {' '.join(conds)}
            LIMIT 20000
        """, *params)
        return JSONResponse({"points": [[float(r["lat"]), float(r["lon"])] for r in rows]})

    @router.get("/admin/api/map-points")
    async def map_points(request: Request, type: str = "sale", rooms: str = "",
                         price_min: float = 0, price_max: float = 0,
                         area_min: float = 0, area_max: float = 0,
                         min_score: int = 0, seller: str = "", market: str = "",
                         price_change: str = "", finish: str = "", cheapest_only: bool = False,
                         has_floorplan: bool = False,
                         offset: int = 0, limit: int = 15000,
                         min_lat: float = 0, max_lat: float = 0,
                         min_lon: float = 0, max_lon: float = 0):
        # публичный (карта на главной без логина); coverage — только админу
        from bot.db.pg import fetch as pg_fetch, fetchval as pg_fetchval2

        tier = await get_user_tier(request)
        if tier == "public" and type == "rental":
            # Публичному тиру аренда на карте не показываем вовсе (только
            # тепловые карты, топ-10 по комнатности и новостройки — всё это
            # про продажу; у аренды нет своего "топ-10 по скору", в таблице
            # нет score-колонки вовсе).
            return JSONResponse({"points": [], "mode": "rental", "count": 0, "no_geo": 0})

        if type == "rental":
            # У аренды нет своих координат — привязываем к центроиду ЖК
            # (по объявлениям продажи того же ЖК). Позиция приблизительная.
            conds, params, i = ["1=1"], [], 1
            if rooms:
                room_list = [int(x) for x in rooms.split(',') if x.strip().isdigit()]
                if room_list:
                    conds.append(f"r.rooms = ANY(${i})"); params.append(room_list); i += 1
            if price_min > 0:
                conds.append(f"r.price >= ${i}"); params.append(int(price_min)); i += 1
            if area_min > 0:
                conds.append(f"r.area >= ${i}"); params.append(area_min); i += 1
            if area_max > 0:
                conds.append(f"r.area <= ${i}"); params.append(area_max); i += 1
            if price_max > 0:
                conds.append(f"r.price <= ${i}"); params.append(int(price_max)); i += 1
            # Раньше центроид ЖК считался LATERAL-подзапросом по apartment_listings
            # НА КАЖДУЮ строку аренды (до 1000 корреляционных сканов самой большой
            # таблицы) — это и было причиной долгой загрузки. Вместо этого считаем
            # центроиды всех ЖК ОДНИМ GROUP BY и джойним как обычную таблицу.
            complex_geo = {r2["cx"]: (float(r2["lat"]), float(r2["lon"]))
                           for r2 in await pg_fetch("""
                SELECT lower(trim(complex_name)) AS cx, AVG(lat) AS lat, AVG(lon) AS lon
                FROM apartment_listings
                WHERE lat IS NOT NULL AND complex_name IS NOT NULL AND btrim(complex_name) != ''
                GROUP BY lower(trim(complex_name))""")}
            rows = await pg_fetch(f"""
                SELECT r.id, r.url, r.price, r.rooms, r.complex_name, r.district, r.found_at,
                       r.lat AS own_lat, r.lon AS own_lon, r.area, r.floor, r.floors_total,
                       r.address, r.photos,
                       ph.old_price AS prev_price, ph.changed_at AS price_changed_at
                FROM rental_listings r
                LEFT JOIN LATERAL (
                    -- Исторический максимум (не последнее изменение) — просили
                    -- показывать "цена упала с максимума до текущей", а не
                    -- только последний шаг изменения.
                    SELECT GREATEST(MAX(old_price), MAX(new_price)) AS old_price, MIN(changed_at) AS changed_at
                    FROM rental_price_history h
                    WHERE h.listing_id = r.id
                ) ph ON TRUE
                WHERE {' AND '.join(conds)}
                  AND (
                    (r.is_active IS NOT FALSE AND r.last_seen > now() - interval '30 days')
                    OR (r.is_active = FALSE AND r.archived_at > now() - interval '30 days')
                  )
                  AND COALESCE(r.is_duplicate, FALSE) = FALSE
                ORDER BY r.found_at DESC LIMIT 1000
            """, *params)
            # Каскад привязки: ЖК -> центроид района -> без привязки.
            district_geo = {r2["district"]: (float(r2["lat"]), float(r2["lon"]))
                            for r2 in await pg_fetch("""
                SELECT district, AVG(lat) AS lat, AVG(lon) AS lon
                FROM apartment_listings
                WHERE lat IS NOT NULL AND district IS NOT NULL AND district != ''
                GROUP BY district""")}
            pts, no_geo = [], 0
            import random as _rnd
            import json as _json_rental

            def _rental_photos(v):
                if isinstance(v, str):
                    try:
                        v = _json_rental.loads(v)
                    except ValueError:
                        v = []
                return (v or [])[:5]

            for r in rows:
                d = dict(r)
                cx_geo = complex_geo.get(str(d.get("complex_name") or "").strip().lower())
                if d.get("own_lat") is not None:
                    # свои координаты с детальной страницы — самая точная привязка
                    lat, lon, binding, jit = float(d["own_lat"]), float(d["own_lon"]), "точно", 0.0
                elif cx_geo is not None:
                    lat, lon, binding, jit = cx_geo[0], cx_geo[1], "ЖК", 0.0005
                else:
                    dg = district_geo.get(d.get("district") or "")
                    if dg:
                        lat, lon, binding, jit = dg[0], dg[1], "район", 0.004
                    else:
                        no_geo += 1
                        continue
                pts.append({
                    "id": d["id"], "url": d["url"] or "",
                    "lat": lat + _rnd.uniform(-jit, jit),
                    "lon": lon + _rnd.uniform(-jit * 1.5, jit * 1.5),
                    "price": d["price"], "rooms": d["rooms"],
                    "complex": d["complex_name"] or "",
                    "binding": binding,
                    "found": d["found_at"].strftime("%d.%m") if d["found_at"] else "",
                    "prev_price": d["prev_price"],
                    "price_changed": d["price_changed_at"].strftime("%d.%m.%Y") if d["price_changed_at"] else None,
                    "area": float(d["area"]) if d.get("area") else None,
                    "floor": d.get("floor"), "floors_total": d.get("floors_total"),
                    "address": d.get("address") or "",
                    "photos": _rental_photos(d.get("photos")),
                })
            return JSONResponse({"points": pts, "mode": "rental",
                                 "count": len(pts), "no_geo": no_geo})

        # Статистика по всей базе не зависит от offset/limit батча — считаем
        # только на первом запросе страницы, чтобы не дублировать тяжёлые COUNT(*)
        # на каждый догоняющий батч.
        total_active = with_coords = 0
        if offset == 0:
            total_active = await pg_fetchval2(
                "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
                "AND COALESCE(is_duplicate, FALSE) = FALSE") or 0
            with_coords = await pg_fetchval2(
                "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
                "AND COALESCE(is_duplicate, FALSE) = FALSE AND lat IS NOT NULL") or 0
        conds, params, i = [], [], 1
        if tier == "public":
            # Задача "общий доступ" (2026-08-12, переформулировано): публичному
            # тиру — весь раздел новостроек (market_type='primary') СО ВСЕМИ
            # фильтрами (комнатность/цена/метраж/скор и т.д. — не режем их,
            # как раньше фиксированным набором top-10+5 застройщиков). Просто
            # жёстко навязываем market_type='primary' поверх остальных
            # условий, даже если клиент явно просил market=secondary.
            conds.append("AND a.market_type = 'primary'")
        if rooms:
            room_list = [int(x) for x in rooms.split(',') if x.strip().isdigit()]
            if room_list:
                conds.append(f"AND rooms = ANY(${i})"); params.append(room_list); i += 1
        if price_min > 0:
            conds.append(f"AND price >= ${i}"); params.append(int(price_min)); i += 1
        if price_max > 0:
            conds.append(f"AND price <= ${i}"); params.append(int(price_max)); i += 1
        if area_min > 0:
            conds.append(f"AND area >= ${i}"); params.append(area_min); i += 1
        if area_max > 0:
            conds.append(f"AND area <= ${i}"); params.append(area_max); i += 1
        if min_score > 0:
            conds.append(f"AND (COALESCE(score_total,0) + COALESCE(zone_bonus,0) + COALESCE(layer_bonus,0) + COALESCE(price_drop_bonus,0)) >= ${i}")
            params.append(min_score); i += 1
        if seller == "owner":
            conds.append("AND is_owner IS TRUE")
        elif seller == "agent":
            conds.append("AND is_owner IS DISTINCT FROM TRUE")
        # Рынок: первичка = market_type='primary'; вторичка = всё остальное
        # (NULL считаем вторичкой — детектор ещё не дошёл до объявления)
        if market == "primary":
            conds.append("AND a.market_type = 'primary'")
        elif market == "secondary":
            conds.append("AND COALESCE(a.market_type, 'secondary') <> 'primary'")
        # Изменение цены сегодня — по последней записи в price_history за сутки.
        if price_change == "dropped_today":
            conds.append("""AND EXISTS (
                SELECT 1 FROM price_history ph2 WHERE ph2.listing_id = a.id
                AND ph2.changed_at >= CURRENT_DATE AND ph2.new_price < ph2.old_price)""")
        elif price_change == "raised_today":
            conds.append("""AND EXISTS (
                SELECT 1 FROM price_history ph2 WHERE ph2.listing_id = a.id
                AND ph2.changed_at >= CURRENT_DATE AND ph2.new_price > ph2.old_price)""")
        # Отделка (см. bot/core/finish_classify.py) — текстовая эвристика,
        # покрывает только объявления с явным сигналом в описании.
        if finish in ("черновая", "с отделкой", "с мебелью"):
            conds.append(f"AND a.finish_type = ${i}"); params.append(finish); i += 1
        # Только объявления с распознанным планом квартиры (см. floorplan_scan.py,
        # заполняет apartment_listings.floorplan_url).
        if has_floorplan:
            conds.append("AND a.floorplan_url IS NOT NULL")
        # Ограничение по текущей видимой области карты — вместо того чтобы
        # тянуть весь город (тысячи объектов), когда пользователь смотрит
        # на один квартал. Приходит от dashboard.html при zoom >= ZOOM_GATE.
        if min_lat and max_lat and min_lon and max_lon:
            conds.append(f"AND a.lat BETWEEN ${i} AND ${i+1} AND a.lon BETWEEN ${i+2} AND ${i+3}")
            params.append(min_lat); params.append(max_lat)
            params.append(min_lon); params.append(max_lon)
            i += 4
        # Пагинация: главная страница подгружает точки батчами (см. dashboard.html
        # applyFilters) — сперва первые ~300 для мгновенной отрисовки, остальное
        # довозится в фоне без блокировки первой отрисовки карты.
        if cheapest_only:
            limit, offset = 10, 0
        else:
            limit = max(1, min(limit, 15000))
            offset = max(0, offset)
        order_by = "a.price ASC" if cheapest_only else "eff_score DESC"
        limit_idx, offset_idx = i, i + 1
        params.append(limit); params.append(offset)
        rows = await pg_fetch(f"""
            SELECT a.id, a.lat, a.lon, a.price, a.rooms, a.area, a.address,
                   a.complex_name, a.url, a.photos, a.market_type, a.geo_source,
                   a.is_owner, a.seller_name, a.year_built, a.views_count,
                   a.description, a.ceiling_height, a.kitchen_area, a.finish_type, a.floor, a.floors_total,
                   a.floorplan_url,
                   a.score_yield, a.score_price_market, a.score_location,
                   a.score_apt_type, a.score_floor, a.score_complex, a.score_supply,
                   a.hex_deal_index, a.deal_confidence, a.yield_pct,
                   EXTRACT(EPOCH FROM (now() - a.first_seen))/86400 AS age_days,
                   (CASE WHEN a.market_type = 'primary' AND a.primary_score_total IS NOT NULL
                         THEN a.primary_score_total
                         ELSE COALESCE(a.score_total,0) END
                    + COALESCE(a.zone_bonus,0)
                    + COALESCE(a.layer_bonus,0)
                    + COALESCE(a.price_drop_bonus,0)) AS eff_score,
                   ph.old_price AS prev_price,
                   ph.changed_at AS price_changed_at,
                   dv.id AS developer_id, dv.name AS developer_name, dv.logo AS developer_logo,
                   cx.photos AS complex_photos
            FROM apartment_listings a
            LEFT JOIN LATERAL (
                -- Исторический максимум, а не последнее изменение — просили
                -- показывать "цена упала с максимума до текущей".
                SELECT GREATEST(MAX(old_price), MAX(new_price)) AS old_price, MIN(changed_at) AS changed_at
                FROM price_history h
                WHERE h.listing_id = a.id
            ) ph ON TRUE
            LEFT JOIN complexes cx ON lower(trim(cx.name)) = lower(trim(a.complex_name))
            LEFT JOIN developers dv ON dv.id = cx.developer_id
            WHERE a.lat IS NOT NULL AND a.lon IS NOT NULL
              AND a.is_active IS NOT FALSE
              AND COALESCE(a.is_duplicate, FALSE) = FALSE
              AND a.last_seen > now() - interval '14 days'
              {"AND a.price > 0" if cheapest_only else ""}
              {' '.join(conds)}
            ORDER BY {order_by}
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """, *params)
        # Настоящий топ-10 сайта — БЕЗ учёта текущих фильтров, отдельным
        # быстрым запросом. Раньше "top" считался как "первые 10 строк ЭТОЙ
        # выдачи" ((offset+idx)<10) — при узком фильтре (например, только
        # снизившие цену сегодня, 7-12 объявлений) это помечало ⭐-топом
        # почти все результаты, что выглядело как "фильтр работает только
        # для топ-10" и путало и звёздочку с реальным рейтингом сайта.
        top10_ids: set = set()
        if offset == 0:
            top10_rows = await pg_fetch("""
                SELECT id FROM apartment_listings a
                WHERE a.lat IS NOT NULL AND a.is_active IS NOT FALSE
                  AND COALESCE(a.is_duplicate, FALSE) = FALSE
                  AND a.last_seen > now() - interval '14 days'
                ORDER BY (CASE WHEN a.market_type = 'primary' AND a.primary_score_total IS NOT NULL
                               THEN a.primary_score_total ELSE COALESCE(a.score_total,0) END
                          + COALESCE(a.zone_bonus,0) + COALESCE(a.layer_bonus,0)
                          + COALESCE(a.price_drop_bonus,0)) DESC
                LIMIT 10
            """)
            top10_ids = {r["id"] for r in top10_rows}
        import json as _json_ph
        def _photos_of(r):
            ph = r["photos"]
            if isinstance(ph, str):
                try:
                    ph = _json_ph.loads(ph)
                except ValueError:
                    ph = []
            return (ph or [])[:5]

        def _complex_photos_of(r):
            ph = r["complex_photos"]
            if isinstance(ph, str):
                try:
                    ph = _json_ph.loads(ph)
                except ValueError:
                    ph = []
            return (ph or [])[:3]

        pts = [{
            "id": r["id"],
            "lat": float(r["lat"]),
            "lon": float(r["lon"]),
            "score": int(r["eff_score"] or 0),
            "photos": _photos_of(r),
            "price": r["price"], "rooms": r["rooms"], "area": float(r["area"] or 0),
            "floor": r["floor"], "floors_total": r["floors_total"],
            "address": r["address"] or "", "complex": r["complex_name"] or "",
            "finish_type": r["finish_type"] or "",
            "developer_id": r["developer_id"], "developer_name": r["developer_name"] or "",
            "developer_logo": r["developer_logo"] or "",
            "year_built": r["year_built"],
            "description": r["description"] or "",
            "ceiling_height": float(r["ceiling_height"]) if r["ceiling_height"] is not None else None,
            "kitchen_area": float(r["kitchen_area"]) if r["kitchen_area"] is not None else None,
            "floorplan_url": r["floorplan_url"] or "",
            "complex_photos": _complex_photos_of(r),
            "url": r["url"] or "",
            "market": r["market_type"] or "",
            "geo": r["geo_source"] or "",
            "score_bd": {
                "yield": r["score_yield"], "price_market": r["score_price_market"],
                "location": r["score_location"], "apt_type": r["score_apt_type"],
                "floor": r["score_floor"], "complex": r["score_complex"],
                "supply": r["score_supply"],
            },
            "is_owner": r["is_owner"] is True,
            "seller_name": r["seller_name"] or "",
            "views": r["views_count"],
            "age": int(r["age_days"] or 0),
            # последняя смена цены (если была) — для попапа на карте
            "prev_price": r["prev_price"],
            "price_changed": r["price_changed_at"].strftime("%d.%m.%Y") if r["price_changed_at"] else None,
            "top": r["id"] in top10_ids,  # настоящий топ-10 сайта, не первые 10 текущей (отфильтрованной) выдачи
            # ── Бейджи "недооценено"/"высокая доходность" (см. задачу) ──────
            # Пороги — топ-10% по городу, утверждены пользователем 2026-08-09:
            # доходность ≥13.9%, "дешевле ожидания" ≥28.8% (перцентили на
            # активных объявлениях на момент утверждения). hex_deal_index —
            # это и есть di=expected/actual из deal_score.py, отдельной
            # колонки под точный % не потребовалось, она уже писалась.
            # deal_confidence≥50 — отсекаем случаи с тонким набором
            # сравнимых объектов, где di может быть шумом, а не сигналом.
            "underpriced_pct": (round((r["hex_deal_index"] - 1) * 100)
                                 if r["hex_deal_index"] and (r["deal_confidence"] or 0) >= 50
                                 and (r["hex_deal_index"] - 1) * 100 >= 28.8 else None),
            "high_yield": bool(r["yield_pct"] and r["yield_pct"] >= 13.9),
        } for idx, r in enumerate(rows)]
        resp = {"points": pts, "count": len(pts), "offset": offset, "limit": limit,
                "has_more": (len(pts) == limit) and not cheapest_only}
        if is_authed(request) and offset == 0:
            dups_active = await pg_fetchval2(
                "SELECT COUNT(*) FROM apartment_listings WHERE is_duplicate = TRUE "
                "AND is_active IS NOT FALSE") or 0
            complexes_total = 0
            unbound_active = 0
            try:
                complexes_total = await pg_fetchval2(
                    "SELECT COUNT(*) FROM complexes") or 0
                unbound_active = await pg_fetchval2("""
                    SELECT COUNT(*) FROM apartment_listings
                    WHERE is_active IS NOT FALSE
                      AND COALESCE(is_duplicate, FALSE) = FALSE
                      AND (complex_name IS NULL OR btrim(complex_name) = '')
                """) or 0
            except Exception:
                pass
            from bot.db import settings as _st
            await _st.load()
            resp["coverage"] = {
                "with_coords": with_coords, "total": total_active,
                "dups": dups_active,
                "complexes": complexes_total,
                "unbound": unbound_active,
                "krisha_total": _st.get_int("KRISHA_TOTAL_FOUND", 0),
                "hex_edge": _st.get_int("HEX_EDGE_M", 50),
            }
        return JSONResponse(resp)

    @router.get("/admin/api/heat-points")
    async def heat_points_api(request: Request):
        """Компактные точки (lat/lon/price/rooms) ВСЕХ активных объявлений
        продажи — для тепловой карты цен на дашборде (drawPriceHeat в
        dashboard.html). Раньше этого эндпоинта не существовало вовсе (фронт
        ссылался на него, получал 404 → heatCache.sale оставался пустым
        массивом), из-за чего гексы тепловой карты продажи не рисовались
        нигде на карте, хотя сами объявления/ЖК были видны."""
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT lat, lon, price, rooms, area, yield_pct
            FROM apartment_listings
            WHERE lat IS NOT NULL AND lon IS NOT NULL
              AND is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
              AND price > 500000
        """)
        return JSONResponse({"points": [{
            "lat": float(r["lat"]), "lon": float(r["lon"]),
            "price": r["price"], "rooms": r["rooms"],
            "area": float(r["area"]) if r["area"] else None,
            "yield_pct": float(r["yield_pct"]) if r["yield_pct"] is not None else None,
        } for r in rows]})

    @router.get("/admin/api/archived-sale-points")
    async def archived_sale_points(request: Request):
        """Последняя цена перед уходом в архив за последний месяц — для
        теплокарты продаж на дашборде: гексагоны без активных объявлений
        сейчас не обязаны быть пустыми, если там недавно что-то продалось.
        Окно — 30 дней (согласовано с archived-rental-points), раньше было
        180 — тепловая карта должна отражать последний месяц рынка, а не
        полгода истории."""
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT id, lat, lon, price, rooms, area, yield_pct
            FROM apartment_listings
            WHERE is_active = FALSE AND archived_at IS NOT NULL
              AND COALESCE(is_duplicate, FALSE) = FALSE
              AND lat IS NOT NULL AND lon IS NOT NULL
              AND price > 500000
              AND archived_at > now() - interval '30 days'
        """)
        return JSONResponse({"points": [{
            "id": r["id"], "lat": float(r["lat"]), "lon": float(r["lon"]),
            "price": r["price"], "rooms": r["rooms"],
            "area": float(r["area"]) if r["area"] else None,
            "yield_pct": float(r["yield_pct"]) if r["yield_pct"] is not None else None,
        } for r in rows]})

    @router.get("/admin/api/archived-rental-points")
    async def archived_rental_points(request: Request):
        """Аналог archived-sale-points для аренды (см. миграцию 040 —
        is_active/archived_at появились у rental_listings только сейчас).
        Последняя цена аренды перед уходом в архив за последний месяц —
        гексагоны без активных объявлений аренды всё ещё показывают, что
        там недавно сдавалось."""
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT id, lat, lon, price, rooms
            FROM rental_listings
            WHERE is_active = FALSE AND archived_at IS NOT NULL
              AND COALESCE(is_duplicate, FALSE) = FALSE
              AND lat IS NOT NULL AND lon IS NOT NULL
              AND price > 0
              AND archived_at > now() - interval '30 days'
        """)
        return JSONResponse({"points": [{
            "id": r["id"], "lat": float(r["lat"]), "lon": float(r["lon"]),
            "price": r["price"], "rooms": r["rooms"],
        } for r in rows]})

    # ── Скор: полное описание модели (сердце проекта) ────────────────────

    @router.get("/admin/score-explained", response_class=HTMLResponse)
    async def score_explained(request: Request):
        # Объединено с /admin/info в одну страницу с вкладками (Общая
        # информация / Скор) — старая ссылка остаётся рабочей.
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return RedirectResponse(url="/admin/info#score", status_code=301)

    # ── Админ панель: единая точка входа для Настроек/Пользователей/Зон/
    # Аналитики — вынесено из главного меню в один пункт справа (см. base.html).
    @router.get("/admin/backup", response_class=HTMLResponse)
    async def backup_page(request: Request):
        """История бэкапов (backup_history) + график размеров по времени."""
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT id, ts, kind, krisha_mb, hype_mb, project_mb, status, note
            FROM backup_history
            ORDER BY ts DESC
            LIMIT 60
        """)
        stats = await pg_fetch("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status='ok') AS ok,
                   MAX(ts) AS last_ts
            FROM backup_history
        """)
        total = stats[0]["total"] if stats else 0
        ok_n = stats[0]["ok"] if stats else 0
        last_ts = stats[0]["last_ts"] if stats else None
        backups = []
        for r in rows:
            d = dict(r)
            for k in ("krisha_mb", "hype_mb", "project_mb"):
                if d.get(k) is not None:
                    d[k] = float(d[k])
            if d.get("ts") is not None:
                d["ts_iso"] = d["ts"].isoformat()
                d["ts_str"] = d["ts"].strftime("%d.%m %H:%M")
            d.pop("ts", None)
            backups.append(d)
        return templates.TemplateResponse("backup.html", {
            "request": request,
            "atab": "backup",
            "backups": backups,
            "total": total,
            "ok_count": ok_n,
            "last_ts": last_ts,
        })

    @router.get("/admin/panel", response_class=HTMLResponse)
    async def admin_panel_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("admin_panel.html", {"request": request})

    # ── Зоны приоритета: карта с рисованием полигонов ────────────────────

    @router.get("/admin/zones", response_class=HTMLResponse)
    async def zones_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("zones.html", {"request": request})

    @router.get("/admin/zones/list")
    async def zones_list(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        rows = await fetch("SELECT id, name, bonus, color, polygon FROM priority_zones ORDER BY id")
        import json as _json
        zones = []
        for r in rows:
            poly = r["polygon"]
            if isinstance(poly, str):
                poly = _json.loads(poly)
            zones.append({"id": r["id"], "name": r["name"], "bonus": r["bonus"],
                          "color": r["color"], "polygon": poly})
        return JSONResponse({"zones": zones})

    @router.post("/admin/zones/save")
    async def zones_save(request: Request):
        """Создание новой зоны ИЛИ обновление существующей (если передан id).
        Баллы — свободные, от −50 до +50 (минус = анти-зона)."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        import json as _json
        data = await request.json()
        zone_id = data.get("id")
        name = (data.get("name") or "Зона").strip()[:100]
        bonus = max(-50, min(50, int(data.get("bonus", 10))))
        color = "#ef4444" if bonus < 0 else data.get("color", "#2563eb")
        polygon = data.get("polygon")  # [[lon,lat], ...] или None при апдейте только баллов
        hexes = data.get("hexes")      # [[[lon,lat]x6], ...] — зона из гексагонов
        if hexes:
            if len(hexes) > 2000:
                return JSONResponse({"error": "слишком много гексов (макс 2000)"},
                                    status_code=400)
            polygon = hexes            # храним зону как набор колец

        from bot.db.pg import fetchval, execute
        if zone_id:  # обновление существующей
            if hexes:
                # добавление новых гексов к уже существующим кольцам зоны
                old = await fetchval(
                    "SELECT polygon FROM priority_zones WHERE id=$1", int(zone_id))
                if isinstance(old, str):
                    old = _json.loads(old)
                old = old or []
                # нормализуем к списку колец и дописываем новые гексы
                if old and not isinstance(old[0][0], list):
                    old = [old]
                polygon = old + hexes
            if polygon and (hexes or len(polygon) >= 3):
                await execute(
                    "UPDATE priority_zones SET name=$2, bonus=$3, color=$4, polygon=$5::jsonb WHERE id=$1",
                    int(zone_id), name, bonus, color, _json.dumps(polygon))
            else:
                await execute(
                    "UPDATE priority_zones SET name=$2, bonus=$3, color=$4 WHERE id=$1",
                    int(zone_id), name, bonus, color)
            logger.info("zone updated: #%s %s (%+d)", zone_id, name, bonus)
            return JSONResponse({"ok": True, "id": int(zone_id)})

        min_ok = len(polygon) >= 1 if hexes else (polygon and len(polygon) >= 3)
        if not polygon or not min_ok:
            return JSONResponse({"error": "polygon too small"}, status_code=400)
        zone_id = await fetchval(
            "INSERT INTO priority_zones (name, bonus, color, polygon) "
            "VALUES ($1, $2, $3, $4::jsonb) RETURNING id",
            name, bonus, color, _json.dumps(polygon),
        )
        logger.info("zone saved: %s (%+d)", name, bonus)
        return JSONResponse({"ok": True, "id": zone_id})

    @router.post("/admin/zones/delete")
    async def zones_delete(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        data = await request.json()
        from bot.db.pg import execute
        await execute("DELETE FROM priority_zones WHERE id = $1", int(data.get("id", 0)))
        return JSONResponse({"ok": True})

    # ── Карточка ЖК: объявления, аренда, ОСИ/УК/чаты ─────────────────────

    @router.get("/admin/complexes/data-audit", response_class=HTMLResponse)
    async def complexes_data_audit(request: Request, limit: int = 300):
        """Таблица по всем ЖК: где какие данные удалось вытащить (застройщик,
        описание, фото, цена/м²), а где нет — чтобы разбираться точечно.
        Публичная страница — админ-элементы скрываются в шаблоне."""
        limit = max(50, min(limit, 5000))
        rows = await fetch("""
            SELECT c.id, c.name, c.developer_id, d.name AS developer_name,
                   c.residents_notes, c.photo_url, c.photos, c.avg_price_m2,
                   c.housing_class, c.year_built, c.korter_url, c.source_info,
                   c.has_parking, c.has_closed_territory, c.has_security,
                   ts.construction_type, ts.facade_type, ts.lifts_brand,
                   ts.ceiling_height_min,
                   COUNT(a.id) FILTER (WHERE a.is_active IS NOT FALSE
                       AND COALESCE(a.is_duplicate, FALSE) = FALSE) AS active_cnt,
                   COUNT(a2.id) AS ever_cnt
            FROM complexes c
            LEFT JOIN developers d ON d.id = c.developer_id
            LEFT JOIN complex_tech_specs ts ON ts.complex_id = c.id
            LEFT JOIN apartment_listings a ON lower(trim(a.complex_name)) = lower(trim(c.name))
                AND a.is_active IS NOT FALSE AND COALESCE(a.is_duplicate, FALSE) = FALSE
            LEFT JOIN apartment_listings a2 ON lower(trim(a2.complex_name)) = lower(trim(c.name))
            WHERE COALESCE(c.is_street, FALSE) = FALSE
            GROUP BY c.id, d.name, ts.construction_type, ts.facade_type, ts.lifts_brand, ts.ceiling_height_min
            ORDER BY active_cnt DESC
            LIMIT $1
        """, limit)
        out = []
        for r in rows:
            d = dict(r)
            out.append({
                "id": d["id"], "name": d["name"], "active_cnt": d["active_cnt"],
                "has_developer": bool(d["developer_name"]),
                "developer_name": d["developer_name"] or "",
                "has_desc": bool(d["residents_notes"]),
                "has_photo": bool(d["photo_url"] or d["photos"]),
                "has_price_m2": bool(d["avg_price_m2"]),
                "has_class": bool(d["housing_class"]),
                "has_year": bool(d["year_built"]),
                "has_korter": bool(d["korter_url"]),
                # Признаки для скоринга класса ЖК (см. Notion "Класс жилья") —
                # конструктив/фасад/лифты/потолки живут в complex_tech_specs
                # (сейчас почти всегда пусто — таблица создана заранее, но
                # источника заполнения ещё нет), закрытость/охрана/паркинг —
                # булевы поля прямо в complexes.
                "has_construction": bool(d["construction_type"]),
                "has_facade": bool(d["facade_type"]),
                "has_lifts": bool(d["lifts_brand"]),
                "has_ceiling": bool(d["ceiling_height_min"]),
                "has_security_data": d["has_parking"] is not None or d["has_closed_territory"] is not None or d["has_security"] is not None,
                # Никогда не привязано ни одного объявления (ни активного, ни
                # архивного) — кандидат на чистку/дубликат/мусорную запись.
                "orphan": d["ever_cnt"] == 0,
                "has_origin": bool(d["korter_url"] or d["source_info"]),
            })
        total_cx = await fetch("SELECT COUNT(*) AS n FROM complexes WHERE COALESCE(is_street, FALSE) = FALSE")
        orphan_cx = await fetch("""
            SELECT COUNT(*) AS n FROM complexes c
            WHERE COALESCE(c.is_street, FALSE) = FALSE
              AND NOT EXISTS (SELECT 1 FROM apartment_listings a WHERE lower(trim(a.complex_name)) = lower(trim(c.name)))
        """)
        orphan_no_origin = await fetch("""
            SELECT COUNT(*) AS n FROM complexes c
            WHERE COALESCE(c.is_street, FALSE) = FALSE
              AND c.korter_url IS NULL AND c.source_info IS NULL
              AND NOT EXISTS (SELECT 1 FROM apartment_listings a WHERE lower(trim(a.complex_name)) = lower(trim(c.name)))
        """)
        return templates.TemplateResponse("complexes_audit.html", {
            "request": request, "rows": out, "limit": limit,
            "total_cx": total_cx[0]["n"] if total_cx else 0,
            "orphan_cx": orphan_cx[0]["n"] if orphan_cx else 0,
            "orphan_no_origin": orphan_no_origin[0]["n"] if orphan_no_origin else 0,
        })

    @router.get("/admin/houses", response_class=HTMLResponse)
    async def admin_houses_page(request: Request, y: str = "no", q: str = "", limit: int = 500):
        """Таблица «Дома по адресам»: все уникальные адреса домов из объявлений,
        с годом постройки (house_years) и привязкой к ЖК. Фильтр y=no — без года."""
        limit = max(50, min(limit, 3000))
        where = []
        params = []
        if y == "no":
            where.append("hy.year_built IS NULL")
        if q:
            params.append(f"%{q}%")
            where.append(f"a.norm_addr LIKE ${len(params)}")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        rows = await fetch(f"""
            SELECT a.norm_addr AS addr,
                   COUNT(*) AS listings_cnt,
                   COUNT(*) FILTER (WHERE a.lat IS NOT NULL) AS with_coords,
                   hy.year_built,
                   a.zhk_name
            FROM (
                SELECT lower(trim(regexp_replace(address, '\\s*—.*$', ''))) AS norm_addr,
                       lat, lon,
                       (SELECT c.name FROM complexes c
                        WHERE lower(trim(c.name)) = lower(trim(al.complex_name))
                          AND c.is_garbage IS NOT TRUE LIMIT 1) AS zhk_name
                FROM apartment_listings al
                WHERE address IS NOT NULL AND address != ''
            ) a
            LEFT JOIN house_years hy ON hy.address = a.norm_addr
            {where_sql}
            GROUP BY a.norm_addr, hy.year_built, a.zhk_name
            ORDER BY listings_cnt DESC
            LIMIT ${len(params) + 1}
        """, *params, limit)

        total = await fetch("SELECT COUNT(DISTINCT lower(trim(regexp_replace(address, '\\s*—.*$', '')))) AS n "
                            "FROM apartment_listings WHERE address IS NOT NULL AND address != ''")
        no_year = await fetch("""
            SELECT COUNT(*) AS n FROM (
                SELECT lower(trim(regexp_replace(address, '\\s*—.*$', ''))) AS addr
                FROM apartment_listings WHERE address IS NOT NULL AND address != ''
                GROUP BY 1
            ) t LEFT JOIN house_years hy ON hy.address = t.addr
            WHERE hy.year_built IS NULL
        """)
        return templates.TemplateResponse("houses.html", {
            "request": request, "rows": rows, "limit": limit,
            "y": y, "q": q,
            "total_houses": total[0]["n"] if total else 0,
            "no_year": no_year[0]["n"] if no_year else 0,
        })

    # "Материал стен" иногда встречается прямо в тексте описания ЖК (не в
    # структурированных complex_materials/tech_specs) — вытаскиваем оттуда в
    # табличку "Материалы и оборудование" вместо дублирования в свободном
    # тексте. Два случая: явная подпись ("материал стен: монолит") и просто
    # упоминание материала рядом со словом "стен" без подписи ("монолитные
    # стены"). Возвращает (найденный_материал | None, текст_без_этого_куска).
    _WALL_MATERIAL_LABELED_RE = re.compile(
        r"материал[а-я]*\s+стен[а-я]*\s*[:—-]\s*([^.\n;]+)", re.IGNORECASE)
    _WALL_MATERIAL_KEYWORDS = ("монолит", "кирпич", "газоблок", "пеноблок",
                               "керамзитобетон", "панель")
    _WALL_MATERIAL_NEARBY_RE = re.compile(
        r"([а-яё]*(?:" + "|".join(_WALL_MATERIAL_KEYWORDS) + r")[а-яё]*)\s+стен[а-я]*",
        re.IGNORECASE)

    def _extract_wall_material(text: str | None) -> tuple[str | None, str | None]:
        if not text:
            return None, text
        m = _WALL_MATERIAL_LABELED_RE.search(text)
        if m:
            material = m.group(1).strip(" .")
            cleaned = (text[:m.start()] + text[m.end():]).strip()
            return material, cleaned
        m = _WALL_MATERIAL_NEARBY_RE.search(text)
        if m:
            material = m.group(1).strip().capitalize()
            cleaned = (text[:m.start()] + text[m.end():]).strip()
            return material, cleaned
        return None, text

    @router.get("/admin/complex/{complex_id}", response_class=HTMLResponse)
    async def complex_detail(request: Request, complex_id: int, nb_rooms: int = -1, nb_all: int = 0):
        # Задача "общий доступ" (2026-08-12): страницы ЖК больше не публичны
        # целиком — всем открыты только новостройки + фильтры + тепловые
        # карты на главной. Админ-элементы (редактирование фото/контактов)
        # для тех, у кого доступ есть, по-прежнему скрываются в шаблоне
        # через is_admin(request).
        tier = await get_user_tier(request)
        if tier == "public":
            return templates.TemplateResponse("access_locked.html", {
                "request": request,
                "title": "Страница ЖК доступна по запросу",
                "message": "Подробная информация по жилым комплексам (описание, удобства, варианты квартир, динамика цен) открыта пользователям с расширенным доступом.",
            })
        from bot.db.pg import fetchrow
        cx = await fetchrow("""
            SELECT c.*, d.name AS developer_name
            FROM complexes c LEFT JOIN developers d ON d.id = c.developer_id
            WHERE c.id = $1
        """, complex_id)
        if not cx:
            return HTMLResponse("<h2>ЖК не найден</h2>", status_code=404)
        if cx.get("is_garbage") is True:
            return HTMLResponse("<h2>ЖК не найден</h2>", status_code=404)
        if cx.get("is_street") is True:
            # Задача "ЖК улицы вообще удалить" (2026-08-12): страница больше
            # не рендерится — раньше показывала баннер "Это улица, а не ЖК"
            # и рендерила карточку целиком. Сама запись в complexes НЕ
            # удаляется (аудит-трейл, см. bot/core/complex_audit.py —
            # чтобы повторный аудит не переоткрывал уже разобранные случаи),
            # просто страница для неё больше недоступна, как у is_garbage.
            return HTMLResponse("<h2>ЖК не найден</h2>", status_code=404)
        cx = dict(cx)
        # photos — JSONB, asyncpg отдаёт как строку; без парсинга шаблон
        # итерировался бы по символам строки, а не по элементам массива
        # (баг: показывало 3 битых <img src="["/"..."> вместо фото).
        if isinstance(cx.get("photos"), str):
            import json as _json_cxph
            try:
                cx["photos"] = _json_cxph.loads(cx["photos"])
            except ValueError:
                cx["photos"] = None

        # "Материал стен" из свободного текста описания -> отдельная строка
        # в "Материалы и оборудование", из самого описания убираем (задача
        # "убрать дублирование материала стен из текста описания").
        wall_material_from_desc = None
        if cx.get("notes"):
            wall_material_from_desc, cx["notes"] = _extract_wall_material(cx["notes"])
        elif cx.get("residents_notes"):
            wall_material_from_desc, cx["residents_notes"] = _extract_wall_material(cx["residents_notes"])

        cname = cx["name"]

        # Координаты ЖК = центроид координат его объявлений; адрес = самый
        # частый адрес среди объявлений (в complexes своих координат нет)
        geo = await fetchrow("""
            SELECT AVG(lat) AS lat, AVG(lon) AS lon
            FROM apartment_listings
            WHERE lower(trim(complex_name)) = lower(trim($1)) AND lat IS NOT NULL
        """, cname)
        addr_row = await fetchrow("""
            SELECT address, COUNT(*) AS cnt FROM apartment_listings
            WHERE lower(trim(complex_name)) = lower(trim($1))
              AND address IS NOT NULL AND address != ''
            GROUP BY address ORDER BY cnt DESC LIMIT 1
        """, cname)

        # Застройщик: справочник developers -> Korter -> Homsters
        developer = cx["developer_name"]
        developer_logo = None
        developer_website = None
        if cx.get("developer_id"):
            _dl = await fetchrow("SELECT logo, website FROM developers WHERE id = $1", cx["developer_id"])
            if _dl:
                developer_logo = _dl["logo"]
                developer_website = _dl["website"]
        if not developer and cx["source_info"]:
            si = cx["source_info"]
            if isinstance(si, str):
                import json as _j
                try:
                    si = _j.loads(si)
                except ValueError:
                    si = {}
            if isinstance(si, dict):
                developer = ((si.get("korter") or {}).get("developer")
                             or (si.get("homsters") or {}).get("developer"))

        sale_listings = await fetch("""
            SELECT id, title, rooms, area, floor, floors_total, price, yield_pct,
                   score_total, url, is_active, first_seen, last_seen, archived_at
            FROM apartment_listings
            WHERE lower(trim(complex_name)) = lower(trim($1))
              AND COALESCE(is_duplicate, FALSE) = FALSE
            ORDER BY is_active DESC NULLS FIRST, score_total DESC NULLS LAST
            LIMIT 60
        """, cname)

        rentals = await fetch("""
            SELECT rooms, price, area, found_at
            FROM rental_listings
            WHERE lower(trim(complex_name)) = lower(trim($1)) AND price > 0
            ORDER BY found_at DESC LIMIT 30
        """, cname)

        # Блок «6 соседних гексагонов» убран со страницы ЖК (см. задачу
        # "убрать hex-блок") — hex_price_cells() больше НЕ вызывается здесь.
        # Функция осталась в модуле нетронутой: её всё ещё использует другой
        # роут (мини-карта в попапе объявления, см. вызов ниже по файлу).

        # Сводка по комнатности: медиана цены продажи, скорость архивации
        stats = await fetch("""
            SELECT rooms,
                   COUNT(*) AS cnt,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price) AS median_price,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price/NULLIF(area,0)) AS median_m2,
                   COUNT(*) FILTER (WHERE is_active IS FALSE) AS sold_cnt,
                   AVG(EXTRACT(EPOCH FROM (archived_at - first_seen))/86400)
                       FILTER (WHERE archived_at IS NOT NULL) AS avg_days_to_sell,
                   MIN(archived_at) FILTER (WHERE archived_at IS NOT NULL) AS first_archived,
                   MAX(archived_at) FILTER (WHERE archived_at IS NOT NULL) AS last_archived
            FROM apartment_listings
            WHERE lower(trim(complex_name)) = lower(trim($1))
              AND COALESCE(is_duplicate, FALSE) = FALSE AND price > 500000
            GROUP BY rooms ORDER BY rooms
        """, cname)

        # Период наблюдения за ЖК (для контекста цифр выше)
        from bot.db.pg import fetchrow as _fetchrow
        obs = await _fetchrow("""
            SELECT MIN(first_seen) AS since, MAX(last_seen) AS until
            FROM apartment_listings
            WHERE lower(trim(complex_name)) = lower(trim($1))
        """, cname)

        # Темп продаж по ЖК: ушло в архив всего / за 30 дней, ср. дней до архива
        pace = await _fetchrow("""
            SELECT COUNT(*) FILTER (WHERE is_active IS FALSE) AS archived_total,
                   COUNT(*) FILTER (WHERE archived_at >= now() - interval '30 days') AS archived_30d,
                   AVG(EXTRACT(EPOCH FROM (archived_at - first_seen))/86400)
                       FILTER (WHERE archived_at IS NOT NULL) AS avg_days_to_sell
            FROM apartment_listings
            WHERE lower(trim(complex_name)) = lower(trim($1))
              AND COALESCE(is_duplicate, FALSE) = FALSE AND price > 500000
        """, cname)

        # Аренда: скорость ухода. "Ушло" = не видели парсером > 3 дней.
        rental_stats = await fetch("""
            SELECT rooms,
                   COUNT(*) AS cnt,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price) AS median_rent,
                   COUNT(*) FILTER (WHERE COALESCE(last_seen, found_at) < now() - interval '3 days') AS gone_cnt,
                   AVG(EXTRACT(EPOCH FROM (COALESCE(last_seen, found_at) - found_at))/86400)
                       FILTER (WHERE COALESCE(last_seen, found_at) < now() - interval '3 days'
                               AND last_seen IS NOT NULL AND last_seen > found_at)
                       AS avg_days_listed
            FROM rental_listings
            WHERE lower(trim(complex_name)) = lower(trim($1)) AND price > 0
            GROUP BY rooms ORDER BY rooms
        """, cname)

        # Карта ЖК убрана со страницы (задача "убери карту", 2026-08-12) —
        # cx_map_points/cx_total больше не нужны, geo остаётся (гейтит блок
        # "Локация ЖК" — локейшн-скор всё ещё считается по координатам).

        # Динамика цены по комнатности (продажа/аренда) теперь грузится с
        # клиента через /admin/api/complex/{id}/price-dynamics (фильтры по
        # комнатности + периоду 3/7/30/90 дней) — см. complex_price_dynamics()
        # ниже. Серверный precompute месячных рядов больше не нужен.

        # Технические характеристики (конструктив/окна/двери/лифты/документы) —
        # почти всегда пусто пока (источника данных ещё нет), но карточка и
        # форма редактирования уже готовы принимать данные, когда появятся.
        from bot.db.pg import execute as _tech_execute
        await _tech_execute("""
            CREATE TABLE IF NOT EXISTS complex_tech_specs (
                complex_id INT PRIMARY KEY REFERENCES complexes(id) ON DELETE CASCADE,
                construction_type TEXT, concrete_class TEXT, rebar_class TEXT,
                facade_type TEXT, insulation_material TEXT, insulation_thickness_mm INT,
                heating_type TEXT, heating_details TEXT, ventilation_type TEXT,
                lifts_brand TEXT, lifts_model TEXT, lifts_count_per_section INT,
                ceiling_height_min NUMERIC, ceiling_height_max NUMERIC,
                developer_bin TEXT, elicense_status TEXT, elicense_checked_at TIMESTAMPTZ,
                docs_psd_expertise_number TEXT, docs_psd_expertise_date DATE,
                docs_apz_number TEXT, docs_apz_date DATE,
                docs_commission_act_number TEXT, docs_commission_act_date DATE,
                notes TEXT, updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        tech_row = await fetchrow(
            "SELECT * FROM complex_tech_specs WHERE complex_id = $1", complex_id)
        tech_specs = dict(tech_row) if tech_row else {}

        # Данные источников (korter/homsters) для блока информации
        cx_sources = {}
        _si = cx["source_info"]
        if _si:
            if isinstance(_si, str):
                import json as _j4
                try:
                    _si = _j4.loads(_si)
                except ValueError:
                    _si = {}
            if isinstance(_si, dict):
                cx_sources = _si

        # Официальные данные homeportal.kz (реестр КЖК) — блок в шаблоне.
        # Раньше hp не передавался, блок «Официальные данные» не рендерился.
        hp_rows = await fetch(
            """SELECT * FROM homeportal_objects
               WHERE matched_complex_id = $1 ORDER BY object_id""", complex_id)
        hp = None
        if hp_rows:
            hp = {"count": len(hp_rows)}
            hp_images: list = []
            hp_apts_total, hp_apts_sold = 0, 0
            hp_rooms_sum = {"1": 0, "2": 0, "3": 0, "4": 0}
            for r in hp_rows:
                rd = dict(r)
                for k, v in rd.items():
                    if v is not None and v != '' and k not in hp:
                        hp[k] = v
                # apartments_total/sold и rooms_1..4 суммируем по всем
                # очередям ЖК (а не берём только первую непустую очередь) —
                # так «продано %» и разбивка по комнатности отражают весь ЖК.
                hp_apts_total += rd.get("apartments_total") or 0
                hp_apts_sold += rd.get("apartments_sold") or 0
                for rk in ("rooms_1", "rooms_2", "rooms_3", "rooms_4"):
                    hp_rooms_sum[rk[-1]] += rd.get(rk) or 0
                imgs = rd.get("images")
                if isinstance(imgs, str):
                    import json as _j5
                    try:
                        imgs = _j5.loads(imgs)
                    except ValueError:
                        imgs = None
                if isinstance(imgs, list):
                    for im in imgs:
                        link = (im or {}).get("image_link") or (im or {}).get("preview_link")
                        if link and link not in hp_images:
                            hp_images.append(link)
            hp["apartments_total_sum"] = hp_apts_total
            hp["apartments_sold_sum"] = hp_apts_sold
            hp["apartments_sold_pct"] = (round(hp_apts_sold / hp_apts_total * 100)
                                          if hp_apts_total else None)
            hp["rooms_mix"] = hp_rooms_sum if sum(hp_rooms_sum.values()) else None
            hp["images"] = hp_images[:8] if hp_images else None
            # is_orda_plus в БД хранится как text '0'/'1'
            hp["is_orda_plus_bool"] = str(hp.get("is_orda_plus") or "0").strip() == "1"

        # Кнопки-ссылки на внешние ресурсы (шапка страницы ЖК): Крыша,
        # homsters, korter, homeportal.kz + сайт застройщика. Серые (нет
        # href), если для этого ЖК нет сопоставления с источником —
        # см. терминал 2026-08-07 фидбек "проверь эти связи".
        # URL-паттерн homeportal.kz/ru/projects/{object_id} подтверждён
        # вручную (открыт живьём, совпадает с нужным ЖК).
        ext_links = {
            "krisha": cx.get("krisha_url") or None,
            "korter": cx.get("korter_url") or (cx_sources.get("korter") or {}).get("url"),
            "homsters": (cx_sources.get("homsters") or {}).get("url"),
            "homeportal": (f"https://homeportal.kz/ru/projects/{hp['object_id']}"
                           if hp and hp.get("object_id") else None),
        }

        # Материалы ЖК: 1) complex_materials (открытые источники), 2) fallback — complex_tech_specs
        materials = None
        try:
            from bot.db.pg import fetch as _mfetch
            mrows = await _mfetch(
                "SELECT facade, walls, windows, elevators, heating, doors, notes, source_name, source_url "
                "FROM complex_materials WHERE complex_id = $1 ORDER BY id", complex_id)
            if mrows:
                materials = [dict(r) for r in mrows]
        except Exception:
            materials = None
        if not materials and tech_specs:
            ts = tech_specs
            fall = []
            def _add(label, val):
                if val and str(val).strip():
                    fall.append({"label": label, "val": str(val).strip(),
                                 "src": "тех. характеристики (Крыша)"})
            _add("Фасад", ts.get("facade_type"))
            _add("Стены / каркас", ts.get("construction_type"))
            _add("Отопление", ts.get("heating_type"))
            _add("Лифты", ts.get("lifts_brand") and (str(ts.get("lifts_brand")) + ((" " + str(ts["lifts_model"])) if ts.get("lifts_model") else "")))
            if ts.get("ceiling_height_min"):
                _add("Высота потолков", str(ts["ceiling_height_min"]) + ("–" + str(ts["ceiling_height_max"]) if ts.get("ceiling_height_max") and ts["ceiling_height_max"] != ts["ceiling_height_min"] else "") + " м")
            if ts.get("notes"):
                _add("Примечания", ts["notes"])
            if fall:
                materials = [{"rows": fall}]

        # Локационный скор ЖК (backlog #31) — бесплатные слои (OSM Overpass +
        # уже посчитанная transport_hexes + demolition_houses), без Yandex/
        # 2GIS (доступа к ним нет). См. bot/core/location_score.py.
        # Локационный скор (backlog #31) считается ОТДЕЛЬНЫМ AJAX-запросом
        # с фронта (см. /admin/api/complex/{id}/location-score ниже), а не
        # тут синхронно — на холодном кэше Overpass (несколько зеркал,
        # retry на каждом) это реально кладёт страницу ЖК целиком (524 от
        # прокси, воспроизведено и исправлено). Отдельный запрос не блокирует
        # рендер страницы и может позволить себе больший таймаут.
        loc_score = None

        # Варианты квартир в доме (новостройки, см. migrations/041_newbuild.sql) —
        # блок под "Материалы и оборудование" на странице ЖК, с фильтром по
        # комнатности (nb_rooms=-1 значит "все"; 0 — отдельный, реальный
        # бакет "комнатность не указана застройщиком", есть у ряда ЖК типа
        # BayPlaza/Sensata, где rooms_amount=0 у самого источника). Раньше
        # сентинел "все" тоже был 0 и совпадал с этим бакетом — кнопка
        # "0-комн" не фильтровала (баг из терминала 2026-08-09). В карточках
        # показываем только 5 штук (по задаче), остальное — по ссылке
        # "показать все" (nb_all=1), полный список никуда не делся.
        # Публичная ручка, is_authed не нужен — та же логика, что у
        # остальных newbuild-эндпоинтов.
        newbuild_units_total = await fetchrow("""
            SELECT COUNT(*) AS cnt FROM newbuild_units
            WHERE complex_id = $1 AND status IN ('available','reserved')
              AND ($2::int = -1 OR rooms = $2)
        """, complex_id, nb_rooms)
        newbuild_units_list = await fetch("""
            SELECT id, rooms, area, floor, floors_total, price, layout_photo_url, status
            FROM newbuild_units
            WHERE complex_id = $1 AND status IN ('available','reserved')
              AND ($2::int = -1 OR rooms = $2)
            ORDER BY rooms, price ASC NULLS LAST
            {limit_clause}
        """.format(limit_clause="" if nb_all else "LIMIT 5"), complex_id, nb_rooms)
        newbuild_rooms_available = await fetch("""
            SELECT DISTINCT rooms FROM newbuild_units
            WHERE complex_id = $1 AND status IN ('available','reserved') AND rooms IS NOT NULL
            ORDER BY rooms
        """, complex_id)

        return templates.TemplateResponse("complex_detail.html", {
            "request": request,
            "cx": dict(cx),
            "cx_sources": cx_sources,
            "geo": {"lat": float(geo["lat"]), "lon": float(geo["lon"])} if geo and geo["lat"] else None,
            "cx_address": addr_row["address"] if addr_row else None,
            "developer": developer,
            "developer_logo": developer_logo,
            "developer_website": developer_website,
            "ext_links": ext_links,
            "sales": [dict(r) for r in sale_listings],
            "rentals": [dict(r) for r in rentals],
            "stats": [dict(r) for r in stats],
            "rental_stats": [dict(r) for r in rental_stats],
            "obs": dict(obs) if obs else {},
            "pace": dict(pace) if pace else {},
            "tech_specs": tech_specs,
            "hp": hp,
            "materials": materials,
            "wall_material_from_desc": wall_material_from_desc,
            "loc_score": loc_score,
            "newbuild_units_list": [dict(r) for r in newbuild_units_list],
            "newbuild_units_total": (newbuild_units_total["cnt"] or 0) if newbuild_units_total else 0,
            "newbuild_rooms_available": [r["rooms"] for r in newbuild_rooms_available],
            "nb_rooms": nb_rooms,
            "nb_all": nb_all,
        })

    # ── Отзывы о ЖК (задача "блок отзывов", 2026-08-07) — смотреть могут
    # все, оставлять — только залогиненные через Telegram (site_session,
    # тот же механизм, что избранное/кабинет). Один отзыв на пользователя
    # на ЖК — повторная отправка обновляет текст/оценку, а не плодит дубли.
    async def _ensure_reviews_table():
        from bot.db.pg import execute as pg_exec
        await pg_exec("""
            CREATE TABLE IF NOT EXISTS complex_reviews (
                id SERIAL PRIMARY KEY,
                complex_id INT NOT NULL REFERENCES complexes(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                rating INT,
                text TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE (complex_id, user_id)
            )
        """)

    @router.get("/admin/api/complex/{complex_id}/reviews")
    async def complex_reviews_get(request: Request, complex_id: int):
        await _ensure_reviews_table()
        from bot.db.pg import fetch as pg_fetch
        from bot.core.site_auth import get_user_by_session
        rows = await pg_fetch("""
            SELECT r.rating, r.text, r.created_at,
                   COALESCE(u.full_name, u.username, 'Пользователь') AS author
            FROM complex_reviews r JOIN users u ON u.user_id = r.user_id
            WHERE r.complex_id = $1
            ORDER BY r.created_at DESC
        """, complex_id)
        me = await get_user_by_session(request.cookies.get("site_session"))
        return JSONResponse({
            "reviews": [{
                "author": r["author"], "rating": r["rating"], "text": r["text"],
                "created_at": r["created_at"].strftime("%d.%m.%Y"),
            } for r in rows],
            "can_post": me is not None,
        })

    @router.post("/admin/api/complex/{complex_id}/reviews")
    async def complex_reviews_post(request: Request, complex_id: int):
        from bot.core.site_auth import get_user_by_session
        me = await get_user_by_session(request.cookies.get("site_session"))
        if not me:
            return JSONResponse({"error": "auth"}, status_code=401)
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "empty"}, status_code=400)
        text = text[:2000]
        rating = body.get("rating")
        try:
            rating = max(1, min(5, int(rating))) if rating else None
        except (TypeError, ValueError):
            rating = None
        await _ensure_reviews_table()
        from bot.db.pg import execute as pg_exec
        await pg_exec("""
            INSERT INTO complex_reviews (complex_id, user_id, rating, text)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (complex_id, user_id)
            DO UPDATE SET rating = EXCLUDED.rating, text = EXCLUDED.text, updated_at = now()
        """, complex_id, me["user_id"], rating, text)
        return JSONResponse({"ok": True})

    @router.get("/admin/api/complex/{complex_id}/location-score")
    async def complex_location_score_api(request: Request, complex_id: int):
        """Локационный скор ЖК (backlog #31) — асинхронно с фронта (см.
        комментарий у complex_detail выше почему не синхронно в самой
        странице). Свой большой таймаут: не блокирует рендер страницы,
        может позволить себе подождать медленный Overpass."""
        from bot.db.pg import fetchrow
        cx = await fetchrow("SELECT name, year_built, district FROM complexes WHERE id = $1", complex_id)
        if not cx:
            return JSONResponse({"error": "not_found"}, status_code=404)
        cx = dict(cx)
        geo = await fetchrow("""
            SELECT AVG(lat) AS lat, AVG(lon) AS lon
            FROM apartment_listings
            WHERE lower(trim(complex_name)) = lower(trim($1)) AND lat IS NOT NULL
        """, cx["name"])
        if not geo or not geo["lat"]:
            return JSONResponse({"error": "no_coords"}, status_code=404)
        try:
            from bot.core.location_score import compute_complex_location_score
            loc_score = await asyncio.wait_for(
                compute_complex_location_score(
                    float(geo["lat"]), float(geo["lon"]),
                    year_built=cx.get("year_built"), district=cx.get("district"),
                ), timeout=90.0)
        except asyncio.TimeoutError:
            return JSONResponse({"error": "timeout"}, status_code=504)
        except Exception as exc:
            logger.warning("location_score API failed for complex %s: %s", complex_id, exc)
            return JSONResponse({"error": "failed"}, status_code=500)
        return JSONResponse(loc_score or {"error": "no_result"})

    @router.get("/admin/api/complex/{complex_id}/price-dynamics")
    async def complex_price_dynamics(request: Request, complex_id: int,
                                      kind: str = "sale", days: int = 90, rooms: str = ""):
        """Данные для графика «Динамика цены по комнатности» на странице ЖК
        (продажа и её аренд-эквивалент) — медиана цены по дням, отдельно на
        каждую комнатность, с фильтром периода (3/7/30/90 дней) и опциональным
        фильтром одной комнатности. Публичный роут (страница ЖК не требует
        логина), как и сам /admin/complex/{id}."""
        from bot.db.pg import fetchrow, fetch as pg_fetch
        cx = await fetchrow("SELECT name FROM complexes WHERE id = $1", complex_id)
        if not cx:
            return JSONResponse({"error": "not_found"}, status_code=404)
        cname = cx["name"]
        if kind == "rental":
            table, time_col = "rental_listings", "found_at"
        else:
            table, time_col = "apartment_listings", "first_seen"
        days = max(1, min(int(days), 365))
        room_cond = ""
        params: list = [cname, str(days)]
        if rooms == "4":
            room_cond = "AND rooms >= 4"
        elif rooms in ("1", "2", "3"):
            room_cond = "AND rooms = $3"
            params.append(int(rooms))
        rows = await pg_fetch(f"""
            SELECT date_trunc('day', {time_col})::date AS d, rooms,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price) AS median_price,
                   COUNT(*) AS n
            FROM {table}
            WHERE lower(trim(complex_name)) = lower(trim($1))
              AND rooms IS NOT NULL AND price > 0 AND {time_col} IS NOT NULL
              AND {time_col} >= now() - ($2 || ' days')::interval
              {room_cond}
            GROUP BY 1, 2
            ORDER BY 1, 2
        """, *params)
        by_rooms: dict = {}
        for r in rows:
            key = str(r["rooms"])
            by_rooms.setdefault(key, []).append({
                "d": r["d"].strftime("%Y-%m-%d"),
                "median_price": float(r["median_price"]),
                "n": r["n"],
            })
        return JSONResponse({"data": by_rooms})

    @router.get("/admin/api/complex/{complex_id}/turnover-dynamics")
    async def complex_turnover_dynamics(request: Request, complex_id: int,
                                         kind: str = "sale", days: int = 90, rooms: str = ""):
        """Данные для графика «Скорость ухода» на странице ЖК — объединяет
        то, что раньше было двумя отдельными несвязанными блоками (таблица
        "Аренда: скорость ухода" + сводка "Продажа" в шапке страницы), в
        один линейный график с тем же UI-паттерном, что и price-dynamics
        (переключатель продажа/аренда, фильтр комнатности, фильтр периода).
        Метрика — среднее число дней в экспозиции до ухода объявления,
        по неделям (день/день не годится: "ушло" — редкое событие, дневные
        бакеты почти всегда пустые или из 1 объявления). Публичный роут,
        как и сам /admin/complex/{id}."""
        from bot.db.pg import fetchrow, fetch as pg_fetch
        cx = await fetchrow("SELECT name FROM complexes WHERE id = $1", complex_id)
        if not cx:
            return JSONResponse({"error": "not_found"}, status_code=404)
        cname = cx["name"]
        days = max(7, min(int(days), 365))
        room_cond = ""
        params: list = [cname, str(days)]
        if rooms == "4":
            room_cond = "AND rooms >= 4"
        elif rooms in ("1", "2", "3"):
            room_cond = "AND rooms = $3"
            params.append(int(rooms))
        if kind == "rental":
            # "Ушло" = не видели парсером > 3 дня (см. rental_stats в
            # complex_detail выше) — тот же критерий, теперь по неделям.
            rows = await pg_fetch(f"""
                SELECT date_trunc('week', COALESCE(last_seen, found_at))::date AS d, rooms,
                       AVG(EXTRACT(EPOCH FROM (COALESCE(last_seen, found_at) - found_at))/86400) AS avg_days,
                       COUNT(*) AS n
                FROM rental_listings
                WHERE lower(trim(complex_name)) = lower(trim($1)) AND price > 0
                  AND rooms IS NOT NULL
                  AND COALESCE(last_seen, found_at) < now() - interval '3 days'
                  AND last_seen IS NOT NULL AND last_seen > found_at
                  AND COALESCE(last_seen, found_at) >= now() - ($2 || ' days')::interval
                  {room_cond}
                GROUP BY 1, 2
                ORDER BY 1, 2
            """, *params)
        else:
            rows = await pg_fetch(f"""
                SELECT date_trunc('week', archived_at)::date AS d, rooms,
                       AVG(EXTRACT(EPOCH FROM (archived_at - first_seen))/86400) AS avg_days,
                       COUNT(*) AS n
                FROM apartment_listings
                WHERE lower(trim(complex_name)) = lower(trim($1))
                  AND COALESCE(is_duplicate, FALSE) = FALSE AND price > 500000
                  AND rooms IS NOT NULL AND archived_at IS NOT NULL
                  AND archived_at >= now() - ($2 || ' days')::interval
                  {room_cond}
                GROUP BY 1, 2
                ORDER BY 1, 2
            """, *params)
        by_rooms: dict = {}
        for r in rows:
            if r["avg_days"] is None:
                continue
            key = str(r["rooms"])
            by_rooms.setdefault(key, []).append({
                "d": r["d"].strftime("%Y-%m-%d"),
                "avg_days": round(float(r["avg_days"]), 1),
                "n": r["n"],
            })
        return JSONResponse({"data": by_rooms})

    @router.post("/admin/complex/{complex_id}/photos")
    async def complex_photos_save(request: Request, complex_id: int):
        """Админ может задать до 3 фото ЖК (внешние URL — как и остальные
        фото в проекте, без загрузки файлов на сервер)."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        data = await request.json()
        photos = [u.strip() for u in (data.get("photos") or []) if u and u.strip()][:3]
        from bot.db.pg import execute
        await execute("ALTER TABLE complexes ADD COLUMN IF NOT EXISTS photos JSONB")
        import json as _json_ph2
        await execute("""
            UPDATE complexes SET photos = $2::jsonb,
                   photo_url = COALESCE($3, photo_url), updated_at = now()
            WHERE id = $1
        """, complex_id, _json_ph2.dumps(photos), photos[0] if photos else None)
        return JSONResponse({"ok": True, "photos": photos})

    @router.post("/admin/complex/{complex_id}/photos/upload")
    async def complex_photos_upload(request: Request, complex_id: int, files: list[UploadFile] = File(...)):
        """Админ загружает свои файлы (до 3) — сохраняются на диск сервера
        и раздаются через /static, вместо внешних URL."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        urls = await _save_uploaded_photos(files, "complexes", complex_id)
        if not urls:
            return JSONResponse({"error": "no valid image files"}, status_code=400)
        from bot.db.pg import execute
        import json as _json_ph3
        await execute("ALTER TABLE complexes ADD COLUMN IF NOT EXISTS photos JSONB")
        await execute("""
            UPDATE complexes SET photos = $2::jsonb,
                   photo_url = COALESCE($3, photo_url), updated_at = now()
            WHERE id = $1
        """, complex_id, _json_ph3.dumps(urls), urls[0])
        return JSONResponse({"ok": True, "photos": urls})

    @router.post("/admin/complex/{complex_id}/contacts")
    async def complex_contacts_save(request: Request, complex_id: int):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        data = await request.json()
        from bot.db.pg import execute
        await execute("""
            UPDATE complexes SET
                osi_contacts = $2, uk_name = $3, uk_contacts = $4,
                chat_links = $5, residents_notes = $6, updated_at = now()
            WHERE id = $1
        """, complex_id,
            (data.get("osi_contacts") or "").strip() or None,
            (data.get("uk_name") or "").strip() or None,
            (data.get("uk_contacts") or "").strip() or None,
            (data.get("chat_links") or "").strip() or None,
            (data.get("residents_notes") or "").strip() or None,
        )
        return JSONResponse({"ok": True})

    # ── Технические характеристики ЖК (конструктив/инженерия/документы) —
    # см. chek-лист застройщика: table complex_tech_specs (1:1 с complexes),
    # плюс детальные complex_walls/windows/doors/concrete_rebar (пока без
    # формы — заполняются вручную/скриптом, когда появится источник данных).
    @router.post("/admin/complex/{complex_id}/tech-specs")
    async def complex_tech_specs_save(request: Request, complex_id: int):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        data = await request.json()
        from bot.db.pg import execute

        def _s(key):
            v = (data.get(key) or "").strip()
            return v or None

        def _d(key):
            v = _s(key)
            if not v:
                return None
            try:
                from datetime import date as _date
                return _date.fromisoformat(v)
            except ValueError:
                return None

        def _i(key):
            v = _s(key)
            try:
                return int(v) if v else None
            except ValueError:
                return None

        def _f(key):
            v = _s(key)
            try:
                return float(v) if v else None
            except ValueError:
                return None

        await execute("""
            INSERT INTO complex_tech_specs (
                complex_id, construction_type, concrete_class, rebar_class,
                facade_type, insulation_material, insulation_thickness_mm,
                heating_type, heating_details, ventilation_type,
                lifts_brand, lifts_model, lifts_count_per_section,
                ceiling_height_min, ceiling_height_max,
                developer_bin, elicense_status, docs_psd_expertise_number,
                docs_psd_expertise_date, docs_apz_number, docs_apz_date,
                docs_commission_act_number, docs_commission_act_date, notes,
                updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24, now())
            ON CONFLICT (complex_id) DO UPDATE SET
                construction_type=$2, concrete_class=$3, rebar_class=$4,
                facade_type=$5, insulation_material=$6, insulation_thickness_mm=$7,
                heating_type=$8, heating_details=$9, ventilation_type=$10,
                lifts_brand=$11, lifts_model=$12, lifts_count_per_section=$13,
                ceiling_height_min=$14, ceiling_height_max=$15,
                developer_bin=$16, elicense_status=$17, docs_psd_expertise_number=$18,
                docs_psd_expertise_date=$19, docs_apz_number=$20, docs_apz_date=$21,
                docs_commission_act_number=$22, docs_commission_act_date=$23, notes=$24,
                updated_at=now()
        """, complex_id, _s("construction_type"), _s("concrete_class"), _s("rebar_class"),
            _s("facade_type"), _s("insulation_material"), _i("insulation_thickness_mm"),
            _s("heating_type"), _s("heating_details"), _s("ventilation_type"),
            _s("lifts_brand"), _s("lifts_model"), _i("lifts_count_per_section"),
            _f("ceiling_height_min"), _f("ceiling_height_max"),
            _s("developer_bin"), _s("elicense_status"), _s("docs_psd_expertise_number"),
            _d("docs_psd_expertise_date"), _s("docs_apz_number"), _d("docs_apz_date"),
            _s("docs_commission_act_number"), _d("docs_commission_act_date"), _s("notes"),
        )
        return JSONResponse({"ok": True})

    # ── Застройщики: список и карточка ────────────────────────────────────

    @router.get("/admin/developers", response_class=HTMLResponse)
    async def developers_page(request: Request):
        """Список застройщиков: крупные (>6 ЖК) — карточками с фото сверху,
        остальные — таблицей снизу (тот же формат, что и Аудит данных по
        ЖК). Фильтр "мин. активных объявлений" убран по запросу — страница
        должна просто показывать всех, а не требовать сначала покрутить
        фильтр."""
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT d.id, d.name, d.logo, d.founded_year, d.website, d.description,
                   d.projects_active, d.projects_total, d.projects_delivered,
                   d.projects_delayed, d.avg_delay_months, d.has_court_cases,
                   d.court_cases_count, d.score_total, d.homsters_slug,
                   COUNT(c.id) AS cx_cnt,
                   COALESCE(SUM(c.listings_count), 0) AS active_cnt,
                   COALESCE(SUM(c.sold_count), 0) AS sold_cnt,
                   photo.photo_url
            FROM developers d
            LEFT JOIN complexes c ON c.developer_id = d.id
                                 AND COALESCE(c.is_street, FALSE) = FALSE
            LEFT JOIN LATERAL (
                SELECT c2.photo_url FROM complexes c2
                WHERE c2.developer_id = d.id AND c2.photo_url IS NOT NULL
                  AND COALESCE(c2.is_street, FALSE) = FALSE
                ORDER BY COALESCE(c2.listings_count, 0) DESC LIMIT 1
            ) photo ON TRUE
            GROUP BY d.id, photo.photo_url
            ORDER BY active_cnt DESC, cx_cnt DESC, d.name
        """)
        card_devs, table_devs = [], []
        for r in rows:
            d = dict(r)
            d.update({
                "has_founded": d["founded_year"] is not None,
                "has_website": bool(d["website"]),
                "has_description": bool(d["description"]),
                "has_score": d["score_total"] is not None,
                "has_homsters": bool(d["homsters_slug"]),
                "has_delay_data": d["avg_delay_months"] is not None,
            })
            (card_devs if d["cx_cnt"] > 6 else table_devs).append(d)
        return templates.TemplateResponse("developers.html", {
            "request": request,
            "card_devs": card_devs,
            "table_devs": table_devs,
            "total": len(rows),
        })

    @router.get("/admin/developer/{dev_id}", response_class=HTMLResponse)
    async def developer_detail(request: Request, dev_id: int):
        from bot.db.pg import fetch, fetchrow
        dev = await fetchrow("SELECT * FROM developers WHERE id = $1", dev_id)
        if not dev:
            return HTMLResponse("<h2>Застройщик не найден</h2>", status_code=404)
        complexes = await fetch("""
            SELECT c.id, c.name, c.district, c.year_built, c.housing_class,
                   c.photo_url, c.lat, c.lon,
                   COALESCE(c.listings_count, 0) AS active_cnt,
                   COALESCE(c.sold_count, 0) AS sold_cnt,
                   c.avg_price_m2,
                   g.avg_days_to_sell, a.address
            FROM complexes c
            LEFT JOIN LATERAL (
                SELECT AVG(EXTRACT(EPOCH FROM (al.archived_at - al.first_seen))/86400)
                         FILTER (WHERE al.archived_at IS NOT NULL) AS avg_days_to_sell
                FROM apartment_listings al
                WHERE lower(trim(al.complex_name)) = lower(trim(c.name))
                  AND COALESCE(al.is_duplicate, FALSE) = FALSE
            ) g ON TRUE
            LEFT JOIN LATERAL (
                SELECT al.address, COUNT(*) AS cnt
                FROM apartment_listings al
                WHERE lower(trim(al.complex_name)) = lower(trim(c.name))
                  AND al.address IS NOT NULL AND al.address != ''
                GROUP BY al.address ORDER BY cnt DESC LIMIT 1
            ) a ON TRUE
            WHERE c.developer_id = $1
              AND COALESCE(c.is_street, FALSE) = FALSE
            ORDER BY active_cnt DESC, sold_cnt DESC
        """, dev_id)
        # Официальные данные homeportal.kz: контакты + проекты застройщика
        # (раньше не передавались, блоки «Официальные контакты» и «Проекты
        # по данным homeportal.kz» не рендерились).
        hp_rows = await fetch(
            """SELECT h.*, c.id AS cx_id, c.name AS cx_name
               FROM homeportal_objects h
               JOIN complexes c ON c.id = h.matched_complex_id
               WHERE c.developer_id = $1
               ORDER BY h.name, h.object_id""", dev_id)
        hp_contact = None
        if hp_rows:
            first = dict(hp_rows[0])
            cand = {
                "bin": first.get("developer_bin"),
                "phone": first.get("developer_phone"),
                "email": first.get("developer_email"),
            }
            if any(cand.values()):
                hp_contact = cand
        hp_projects = [dict(r) for r in hp_rows] or None

        return templates.TemplateResponse("developer_detail.html", {
            "request": request,
            "dev": dict(dev),
            "complexes": [dict(r) for r in complexes],
            "hp_contact": hp_contact,
            "hp_projects": hp_projects,
        })

    # ── Дороги (кол-во полос) — для предварительной карты шума ─────────────

    @router.get("/admin/api/city-roads")
    async def city_roads(request: Request):
        """Точки дорог для карты шума. Массивы [lat, lon, lanes, highway]
        вместо объектов с именованными ключами — при 30м-семплировании вдоль
        каждой дороги (см. road_import.py) это ~39к точек, и повторяющиеся
        ключи "lat"/"lon"/"lanes"/"highway" в каждой из них раздували ответ
        почти вдвое против самих данных (~2.4МБ -> заметная пауза перед тем,
        как вообще можно начать считать гексы шума)."""
        from bot.db.pg import fetch as pg_fetch
        try:
            rows = await pg_fetch("SELECT lat, lon, lanes, highway FROM city_roads")
        except Exception:
            rows = []
        return JSONResponse({"roads": [[float(r["lat"]), float(r["lon"]), r["lanes"], r["highway"]] for r in rows]})

    # ── Детали объявления для модального окна на дашборде ──────────────────

    @router.get("/admin/api/listing/{listing_id}")
    async def api_listing_detail(request: Request, listing_id: str):
        """Полные данные объявления для модалки (фото, адрес, торг).
        Публичный (как и сама карта) — ничего чувствительного тут нет."""
        from bot.db.pg import fetchrow as pg_fetchrow, fetch as pg_fetch
        from bot.core.bargain import get_comparables, analyze_bargain
        from datetime import datetime as _dt, timezone as _tz
        import json as _json_ld

        row = await pg_fetchrow("SELECT * FROM apartment_listings WHERE id = $1", listing_id)
        if not row:
            return JSONResponse({"error": "not_found"}, status_code=404)
        l = dict(row)

        photos = l.get("photos")
        if isinstance(photos, str):
            try:
                photos = _json_ld.loads(photos)
            except ValueError:
                photos = []

        comps, comps_meta = await get_comparables(
            lat=float(l["lat"]) if l.get("lat") is not None else None,
            lon=float(l["lon"]) if l.get("lon") is not None else None,
            rooms=l.get("rooms"), area=l.get("area"), current_price=l.get("price", 0),
            complex_name=l.get("complex_name"), district=l.get("district"),
            exclude_id=listing_id,
        )
        bargain = analyze_bargain(l.get("price", 0), comps, l.get("is_owner"), l.get("is_urgent") is True, comps_meta)

        from bot.core.listing_intel import build_negotiation_points, build_seller_questions, compute_similar_listings
        negotiation_points = build_negotiation_points(l, bargain, len(comps))
        seller_questions = build_seller_questions(l)
        tier = await get_user_tier(request)
        # Публичному тиру не показываем "похожие рядом" (карта+список) —
        # ни в попапе с карты, ни в попапе, открытом через "вставить ссылку
        # с Крыши" (задача "3 уровня доступа", 2026-08-07). Не считаем
        # вовсе, а не просто прячем в шаблоне — не тратим время на compute
        # для тира, которому это всё равно не покажется.
        similar_listings = [] if tier == "public" else await compute_similar_listings(l, listing_id, limit=10)

        layers = l.get("layer_details")
        if isinstance(layers, str):
            try:
                layers = _json_ld.loads(layers)
            except ValueError:
                layers = None

        ai_analysis = l.get("ai_analysis")
        if isinstance(ai_analysis, str):
            try:
                ai_analysis = _json_ld.loads(ai_analysis)
            except ValueError:
                ai_analysis = None

        # Фото ЖК — для галереи в модалке объявления (переиспользуем то же
        # поле photos, что и на карточке ЖК)
        complex_photos = []
        if l.get("complex_name"):
            cx_row = await pg_fetchrow(
                "SELECT photos, photo_url FROM complexes WHERE lower(trim(name)) = lower(trim($1)) LIMIT 1",
                l["complex_name"])
            if cx_row:
                cxp = cx_row["photos"]
                if isinstance(cxp, str):
                    try:
                        cxp = _json_ld.loads(cxp)
                    except ValueError:
                        cxp = None
                complex_photos = cxp or ([cx_row["photo_url"]] if cx_row.get("photo_url") else [])

        # Лента "рядом" — 3 ближайших активных объявления по прямому расстоянию
        nearby = []
        if l.get("lat") is not None and l.get("lon") is not None:
            nb_rows = await pg_fetch("""
                SELECT id, url, price, rooms, area, photos
                FROM apartment_listings
                WHERE lat IS NOT NULL AND lon IS NOT NULL
                  AND is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
                  AND id != $3
                ORDER BY (lat - $1)^2 + (lon - $2)^2 ASC LIMIT 3
            """, float(l["lat"]), float(l["lon"]), listing_id)
            for nb in nb_rows:
                nb_photos = nb["photos"]
                if isinstance(nb_photos, str):
                    try:
                        nb_photos = _json_ld.loads(nb_photos)
                    except ValueError:
                        nb_photos = []
                nearby.append({
                    "id": nb["id"], "url": nb.get("url") or "",
                    "price": nb.get("price"), "rooms": nb.get("rooms"),
                    "area": float(nb["area"]) if nb.get("area") else None,
                    "photo": (nb_photos or [None])[0],
                })

        return JSONResponse({
            "id": l["id"], "url": l.get("url") or "",
            "price": l.get("price"), "rooms": l.get("rooms"),
            "market": l.get("market_type") or "secondary",
            "area": float(l["area"]) if l.get("area") else None,
            "floor": l.get("floor"), "floors_total": l.get("floors_total"),
            "address": l.get("address") or "", "district": l.get("district") or "",
            "lat": float(l["lat"]) if l.get("lat") is not None else None,
            "lon": float(l["lon"]) if l.get("lon") is not None else None,
            "complex_name": l.get("complex_name") or "",
            "complex_photos": complex_photos,
            "geo": l.get("geo_source") or "",
            "photos": photos or [],
            "seller_name": l.get("seller_name") or "",
            "is_owner": l.get("is_owner") is True,
            "year_built": l.get("year_built"),
            "views_count": l.get("views_count"),
            "floorplan_url": l.get("floorplan_url") or "",
            # ceiling_height не отдавался тут вообще — d.ceiling_height в
            # модалке (dashboard.html) был мёртвым полем (всегда undefined),
            # плашка "потолок N м" никогда не показывалась. kitchen_area —
            # новое поле (см. задачу "кухня в парсерах продажи").
            "ceiling_height": float(l["ceiling_height"]) if l.get("ceiling_height") is not None else None,
            "kitchen_area": float(l["kitchen_area"]) if l.get("kitchen_area") is not None else None,
            "ai_analysis": ai_analysis,
            "similar": similar_listings,
            "tier": tier,
            "layers": layers,
            "description": l.get("description") or "",
            "first_seen": l["first_seen"].strftime("%d.%m.%Y") if l.get("first_seen") else None,
            "age": int((_dt.now(_tz.utc) - l["first_seen"]).days) if l.get("first_seen") else None,
            "bargain": {
                "discount_pct": bargain.get("discount_pct") or 0,
                "target_price": bargain.get("target_price"),
                "median_price": bargain.get("median_price"),
                "comparables_cnt": bargain.get("comparables_cnt") or 0,
                "recommendation": bargain.get("recommendation") or "",
                "method": bargain.get("method"),
                "class_note": bargain.get("class_note"),
            },
            "nearby": nearby,
            "deal_score": (lambda hd: {
                "deal": hd.get("deal"), "confidence": hd.get("confidence"),
            } if hd else None)(
                (lambda v: (_json_ld.loads(v) if isinstance(v, str) else v) if v else None)(l.get("hex_details"))
            ),
            # То же, что показывает страница /admin/analytics/{id} — доходность
            # и разбивка скора по компонентам, теперь дублируется и в модалке
            # на карте, чтобы не заставлять переходить на отдельную страницу.
            "est_rent": l.get("est_rent"),
            "yield_pct": l.get("yield_pct"),
            # net_yield_pct — доходность с поправкой на вакантность/налог/
            # расходы на покупку (см. Notion "Расчет доходности"), gross
            # выше остаётся для обратной совместимости со старым скорингом.
            "net_yield_pct": l.get("net_yield_pct"),
            "payback_years": l.get("payback_years"),
            # ── Бейджи "недооценено"/"высокая доходность" — те же пороги
            # (топ-10%), что и в /admin/api/map-points, см. комментарий там.
            "underpriced_pct": (round((l["hex_deal_index"] - 1) * 100)
                                 if l.get("hex_deal_index") and (l.get("deal_confidence") or 0) >= 50
                                 and (l["hex_deal_index"] - 1) * 100 >= 28.8 else None),
            "high_yield": bool(l.get("yield_pct") and l["yield_pct"] >= 13.9),
            "rent_source": l.get("rent_source") or "",
            "zone_name": l.get("zone_name") or "",
            "zone_bonus": l.get("zone_bonus"),
            "score_breakdown": {
                "yield": l.get("score_yield") or 0,
                "price_market": l.get("score_price_market") or 0,
                "location": l.get("score_location") or 0,
                "apt_type": l.get("score_apt_type") or 0,
                "floor": l.get("score_floor") or 0,
                "complex": l.get("score_complex") or 0,
                "supply": l.get("score_supply") or 0,
            },
            "negotiation_points": negotiation_points,
            "seller_questions": seller_questions,
        })

    # ── История цены объявления ───────────────────────────────────────────

    @router.get("/admin/api/price-history/{listing_id}")
    async def api_price_history(request: Request, listing_id: str):
        """История цены для карточки/попапа на карте.
        Публичный (как и сама карта) — ничего чувствительного тут нет."""
        from bot.db.pg import fetch as pg_fetch, fetchrow as pg_fetchrow
        rows = await pg_fetch("""
            SELECT old_price, new_price, changed_at
            FROM price_history
            WHERE listing_id = $1
            ORDER BY changed_at ASC
        """, listing_id)
        cur = await pg_fetchrow(
            "SELECT price, first_seen FROM apartment_listings WHERE id = $1",
            listing_id)
        points = []
        # стартовая точка — цена при первом появлении в базе
        if cur and cur["first_seen"]:
            first_price = rows[0]["old_price"] if rows else cur["price"]
            points.append({"at": cur["first_seen"].strftime("%d.%m.%Y"),
                           "price": first_price})
        for r in rows:
            points.append({"at": r["changed_at"].strftime("%d.%m.%Y"),
                           "price": r["new_price"]})
        return JSONResponse({
            "points": points,
            "current": cur["price"] if cur else None,
            "changes": len(rows),
        })

    @router.get("/admin/api/no-photo-history")
    async def no_photo_history(request: Request, days: int = 30):
        """Снимки no_photo_stats_history — сколько активных объявлений без
        фото во времени (записывается раз в цикл парсера продаж)."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT at, total_active, no_photo
            FROM no_photo_stats_history
            WHERE at > now() - ($1 || ' days')::interval
            ORDER BY at ASC
        """, str(days))
        return JSONResponse({"points": [{
            "at": r["at"].strftime("%d.%m %H:%M"),
            "total_active": r["total_active"],
            "no_photo": r["no_photo"],
        } for r in rows]})

    @router.get("/admin/api/floor-history")
    async def floor_history(request: Request, days: int = 30):
        """Снимки floor_stats_history — доля активных объявлений с известным
        этажом во времени (записывается раз в цикл парсера продаж, см.
        service_apartments.py). Этаж, как и фото, приходит только с детальной
        страницы объявления, поэтому покрытие растёт постепенно."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT at, total_active, with_floor
            FROM floor_stats_history
            WHERE at > now() - ($1 || ' days')::interval
            ORDER BY at ASC
        """, str(days))
        return JSONResponse({"points": [{
            "at": r["at"].strftime("%d.%m %H:%M"),
            "total_active": r["total_active"],
            "with_floor": r["with_floor"],
            "pct": round(100 * r["with_floor"] / r["total_active"], 1) if r["total_active"] else 0,
        } for r in rows]})

    @router.get("/admin/api/views-history")
    async def views_history(request: Request, days: int = 1):
        """Снимки views_stats_history — суммарные просмотры по комнатности
        во времени (записывается раз в цикл парсера продаж, см.
        service_apartments.py). views_count накопительный, так что график
        показывает динамику накопленного интереса, а не просмотры "за день"."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT at, views_1, views_2, views_3, views_4p
            FROM views_stats_history
            WHERE at > now() - ($1 || ' days')::interval
            ORDER BY at ASC
        """, str(days))
        return JSONResponse({"points": [{
            "at": r["at"].strftime("%d.%m %H:%M"),
            "v1": r["views_1"] or 0,
            "v2": r["views_2"] or 0,
            "v3": r["views_3"] or 0,
            "v4p": r["views_4p"] or 0,
        } for r in rows]})

    @router.get("/admin/api/price-drops-history")
    async def price_drops_history(request: Request, days: int = 30):
        """Сколько объявлений снизили цену по дням, по комнатности (1/2/3+),
        и на какую суммарную сумму — для графика на /admin/analytics."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT ph.changed_at::date AS d,
                   CASE WHEN a.rooms >= 3 THEN 3 ELSE COALESCE(a.rooms, 0) END AS room_bucket,
                   COUNT(DISTINCT ph.listing_id) AS cnt,
                   SUM(ph.old_price - ph.new_price) AS total_drop
            FROM price_history ph
            JOIN apartment_listings a ON a.id = ph.listing_id
            WHERE ph.new_price < ph.old_price
              AND ph.changed_at > now() - ($1 || ' days')::interval
            GROUP BY 1, 2
            ORDER BY 1
        """, str(days))
        days_set = sorted({r["d"] for r in rows})
        series = {"1": [], "2": [], "3": []}
        amounts = {"1": [], "2": [], "3": []}
        by_day = {}
        for r in rows:
            by_day.setdefault(r["d"], {})[str(r["room_bucket"])] = (r["cnt"], r["total_drop"] or 0)
        for d in days_set:
            for k in ("1", "2", "3"):
                cnt, amt = by_day.get(d, {}).get(k, (0, 0))
                series[k].append(cnt)
                amounts[k].append(int(amt))
        return JSONResponse({
            "days": [d.strftime("%d.%m") for d in days_set],
            "series": series,
            "amounts": amounts,
        })

    @router.get("/admin/api/price-trend-history")
    async def price_trend_history(request: Request, days: int = 30, rooms: str = ""):
        """Динамика роста/снижения цен по дням (сколько объявлений подняли
        цену и сколько снизили, + средняя сумма изменения) — для графика
        на /admin/analytics/prices. rooms: "" (все), "1".."3", "4" (4+)."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        room_cond = ""
        params: list = [str(days)]
        if rooms == "4":
            room_cond = "AND a.rooms >= 4"
        elif rooms in ("1", "2", "3"):
            room_cond = "AND a.rooms = $2"
            params.append(int(rooms))
        rows = await pg_fetch(f"""
            SELECT ph.changed_at::date AS d,
                   COUNT(*) FILTER (WHERE ph.new_price > ph.old_price) AS cnt_up,
                   COUNT(*) FILTER (WHERE ph.new_price < ph.old_price) AS cnt_down,
                   AVG(ph.new_price - ph.old_price) FILTER (WHERE ph.new_price > ph.old_price) AS avg_up,
                   AVG(ph.old_price - ph.new_price) FILTER (WHERE ph.new_price < ph.old_price) AS avg_down
            FROM price_history ph
            JOIN apartment_listings a ON a.id = ph.listing_id
            WHERE ph.changed_at > now() - ($1 || ' days')::interval
              {room_cond}
            GROUP BY 1
            ORDER BY 1
        """, *params)
        return JSONResponse({
            "days": [r["d"].strftime("%d.%m") for r in rows],
            "cnt_up": [r["cnt_up"] or 0 for r in rows],
            "cnt_down": [r["cnt_down"] or 0 for r in rows],
            "avg_up": [int(r["avg_up"] or 0) for r in rows],
            "avg_down": [int(r["avg_down"] or 0) for r in rows],
        })

    @router.get("/admin/api/price-trend-listings")
    async def price_trend_listings(request: Request, days: int = 30, rooms: str = ""):
        """Конкретные объявления, из которых сложился график на
        /admin/analytics/prices — для правой колонки (список объектов,
        от самых больших изменений цены к самым маленьким)."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        room_cond = ""
        params: list = [str(days)]
        if rooms == "4":
            room_cond = "AND a.rooms >= 4"
        elif rooms in ("1", "2", "3"):
            room_cond = "AND a.rooms = $2"
            params.append(int(rooms))
        rows = await pg_fetch(f"""
            SELECT ph.listing_id, ph.old_price, ph.new_price, ph.changed_at,
                   a.rooms, a.area, a.url, a.complex_name, a.address
            FROM price_history ph
            JOIN apartment_listings a ON a.id = ph.listing_id
            WHERE ph.changed_at > now() - ($1 || ' days')::interval
              AND ph.old_price IS NOT NULL AND ph.new_price IS NOT NULL
              AND ph.old_price != ph.new_price
              {room_cond}
            ORDER BY abs(ph.new_price - ph.old_price) DESC
            LIMIT 300
        """, *params)
        return JSONResponse({"listings": [{
            "id": r["listing_id"], "rooms": r["rooms"], "area": r["area"],
            "old_price": r["old_price"], "new_price": r["new_price"],
            "delta": r["new_price"] - r["old_price"],
            "changed_at": r["changed_at"].strftime("%d.%m.%Y"),
            "url": r["url"], "complex_name": r["complex_name"], "address": r["address"],
        } for r in rows]})

    @router.get("/admin/api/price-by-district")
    async def price_by_district(request: Request):
        """Средняя цена за м² по комнатности и району (задача "график средней
        цены по комнатности по районам") — используем официальные 6 районов
        Астаны (apartment_listings.district), т.к. неформальные зоны вроде
        "Туран"/"Мангилик" — это названия проспектов/микрорайонов ВНУТРИ
        Есильского района без готовых границ в базе (только один вручную
        нарисованный полигон в priority_zones), а не отдельный столбец с
        надёжным покрытием. Официальные районы дают то же сравнение "какая
        часть города дороже/дешевле", просто на другой, но реальной сетке."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT district,
                   LEAST(rooms, 4) AS room_bucket,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price/NULLIF(area,0)) AS median_m2,
                   COUNT(*) AS cnt
            FROM apartment_listings
            WHERE is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
              AND price > 500000 AND area > 0 AND rooms BETWEEN 1 AND 10
              AND district IN ('Есильский р-н', 'Алматы р-н', 'Сарыарка р-н',
                                'Сарайшык р-н', 'р-н Байконур', 'Нура р-н')
            GROUP BY district, LEAST(rooms, 4)
            HAVING COUNT(*) >= 3
        """)
        return JSONResponse({"rows": [{
            "district": r["district"], "rooms": r["room_bucket"],
            "median_m2": round(r["median_m2"]) if r["median_m2"] else None,
            "count": r["cnt"],
        } for r in rows]})

    # ── Ушедшие в архив: динамика по дням, по комнатности ──────────────────

    @router.get("/admin/archived", response_class=HTMLResponse)
    async def archived_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import fetchval as pg_fv
        await app_settings.load()
        stats = {
            "archived_total": await pg_fv(
                "SELECT COUNT(*) FROM apartment_listings WHERE archived_at IS NOT NULL") or 0,
            "archived_today": await pg_fv(
                "SELECT COUNT(*) FROM apartment_listings WHERE archived_at::date = CURRENT_DATE") or 0,
            "archived_7d": await pg_fv(
                "SELECT COUNT(*) FROM apartment_listings "
                "WHERE archived_at > now() - interval '7 days'") or 0,
            # Покрытие/частота проверки на архивность (см. bot/core/archive_check.py):
            # каждый цикл продаж (~50-80 мин) проверяет ARCHIVE_CHECK_BATCH
            # лучших по скору активных объявлений, у которых archive_checked_at
            # либо NULL, либо старше 24ч (не чаще раза в сутки на объявление).
            "total_active": await pg_fv(
                "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE") or 0,
            "never_checked": await pg_fv(
                "SELECT COUNT(*) FROM apartment_listings "
                "WHERE is_active IS NOT FALSE AND archive_checked_at IS NULL") or 0,
            "checked_24h": await pg_fv(
                "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
                "AND archive_checked_at > now() - interval '24 hours'") or 0,
            "stale_over_7d": await pg_fv(
                "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
                "AND archive_checked_at < now() - interval '7 days'") or 0,
            "archive_batch": app_settings.get_int("ARCHIVE_CHECK_BATCH", 150),
        }
        return templates.TemplateResponse("archived.html", {
            "request": request, "atab": "archived", "stats": stats,
        })

    @router.get("/admin/api/archived-price-m2-history")
    async def archived_price_m2_history(request: Request, days: int = 30, rooms: str = ""):
        """Медиана цены/м² среди объявлений, ушедших в архив, по дням —
        для графика на /admin/archived. Раньше фронтенд ссылался на этот
        путь, а обработчика не существовало вовсе (см. тот же класс бага,
        что и с /admin/api/heat-points) — график был всегда пуст."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        room_cond = ""
        params: list = [str(days)]
        if rooms:
            room_cond = "AND rooms = $2"
            params.append(int(rooms))
        rows = await pg_fetch(f"""
            SELECT archived_at::date AS d,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price / NULLIF(area, 0)) AS median_m2,
                   COUNT(*) AS cnt
            FROM apartment_listings
            WHERE archived_at > now() - ($1 || ' days')::interval
              AND price > 0 AND area > 0
              {room_cond}
            GROUP BY 1
            ORDER BY 1
        """, *params)
        return JSONResponse({"points": [{
            "d": r["d"].strftime("%d.%m"),
            "median_m2": round(r["median_m2"]) if r["median_m2"] is not None else None,
            "cnt": r["cnt"],
        } for r in rows]})

    @router.get("/admin/api/archived-history")
    async def archived_history(request: Request, days: int = 30):
        """Сколько объявлений ушло в архив по дням, отдельно по комнатности
        (1/2/3+, продажа) — для графика на /admin/archived. Показывает реальный
        охват archive_check (см. ARCHIVE_CHECK_BATCH в настройках), а не момент
        фактического снятия объявления с Крыши — это дата, когда МЫ это
        заметили при следующей проверке.

        Плюс отдельная серия "аренда": у rental_listings нет явного
        archived_at (нет детального парсера, который бы проверял страницу на
        пометку "В архиве") — как и в карточке ЖК ("скорость ухода аренды"),
        считаем объявление ушедшим, если его не видели 3+ дня подряд после
        last_seen, и датой ухода — last_seen + 3 дня."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT archived_at::date AS d,
                   CASE WHEN rooms >= 3 THEN 3 ELSE COALESCE(rooms, 0) END AS room_bucket,
                   COUNT(*) AS cnt
            FROM apartment_listings
            WHERE archived_at > now() - ($1 || ' days')::interval
            GROUP BY 1, 2
            ORDER BY 1
        """, str(days))
        rental_rows = await pg_fetch("""
            SELECT (last_seen::date + interval '3 days')::date AS d,
                   CASE WHEN rooms >= 3 THEN 3 ELSE COALESCE(rooms, 0) END AS room_bucket,
                   COUNT(*) AS cnt
            FROM rental_listings
            WHERE last_seen < now() - interval '3 days'
              AND last_seen::date + interval '3 days' > now() - ($1 || ' days')::interval
            GROUP BY 1, 2
            ORDER BY 1
        """, str(days))
        by_day = {}
        for r in rows:
            by_day.setdefault(r["d"], {})[str(r["room_bucket"])] = r["cnt"]
        rental_by_day = {}
        for r in rental_rows:
            rental_by_day.setdefault(r["d"], {})[str(r["room_bucket"])] = r["cnt"]
        days_set = sorted(set(by_day) | set(rental_by_day))
        series = {"1": [], "2": [], "3": [], "rental_1": [], "rental_2": [], "rental_3": []}
        for d in days_set:
            for k in ("1", "2", "3"):
                series[k].append(by_day.get(d, {}).get(k, 0))
                series["rental_" + k].append(rental_by_day.get(d, {}).get(k, 0))
        return JSONResponse({
            "days": [d.strftime("%d.%m") for d in days_set],
            "series": series,
        })

    @router.get("/admin/api/archived-hex-points")
    async def archived_hex_points(request: Request, type: str = "sale", rooms: str = "",
                                  days: int = 180):
        """Точки для гекс-карт на /admin/archived: последняя цена и скорость
        ухода (дни от появления до архива) для каждого ушедшего объявления.
        type: sale|rental. rooms: "" (все), "1".."3", "4" (4+).
        days: за какой период считать "ушедшим" (1/3/7/30/180 — фильтр в UI).
        Бакетирование в гексагоны и агрегация (среднее по гексу) — на клиенте,
        тем же кодом, что и тепловые карты на дашборде."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        days = days if days in (1, 3, 7, 30, 180) else 180
        from bot.db.pg import fetch as pg_fetch
        room_cond = ""
        params: list = [str(days)]
        if rooms == "4":
            room_cond = "AND rooms >= 4"
        elif rooms in ("1", "2", "3"):
            room_cond = "AND rooms = $2"
            params.append(int(rooms))
        if type == "rental":
            rows = await pg_fetch(f"""
                SELECT lat, lon, price, rooms,
                       EXTRACT(EPOCH FROM (last_seen - found_at)) / 86400.0 AS days
                FROM rental_listings
                WHERE last_seen < now() - interval '3 days'
                  AND last_seen > now() - ($1 || ' days')::interval
                  AND lat IS NOT NULL AND lon IS NOT NULL
                  AND price > 0
                  {room_cond}
            """, *params)
        else:
            rows = await pg_fetch(f"""
                SELECT lat, lon, price, rooms,
                       EXTRACT(EPOCH FROM (archived_at - first_seen)) / 86400.0 AS days
                FROM apartment_listings
                WHERE is_active = FALSE AND archived_at IS NOT NULL
                  AND archived_at > now() - ($1 || ' days')::interval
                  AND lat IS NOT NULL AND lon IS NOT NULL
                  AND price > 0
                  {room_cond}
            """, *params)
        return JSONResponse({"points": [{
            "lat": float(r["lat"]), "lon": float(r["lon"]),
            "price": r["price"], "rooms": r["rooms"],
            "days": round(float(r["days"]), 1) if r["days"] is not None and r["days"] >= 0 else None,
        } for r in rows]})

    # ── Аналитика просмотров (krisha-viewcount.service, см. вкладку Инфо) ──
    # HTML-страницы /admin/analytics/views и /admin/analytics/floors теперь
    # объявлены в bot/admin_web.py (ДО catch-all /admin/analytics/{listing_id},
    # который иначе перехватывал их как несуществующий listing_id — см. фикс).
    # Тут остаются только их API-эндпоинты.

    @router.get("/admin/api/views-points")
    async def views_points(request: Request, days: int = 7):
        """Объявления с известным числом просмотров (views_count, см.
        service_viewcount.py) — для гекс-карты медианных просмотров и топ-50.
        views_count — накопительный счётчик с даты публикации (Крыша не даёт
        историю по дням), поэтому "за N дней" здесь = первые N дней после
        публикации, а не срез накопленного графика: так число просмотров
        листинга целиком укладывается в выбранное окно, а не искажено более
        старыми объявлениями с заведомо большим накопленным счётчиком."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        days = days if days in (1, 3, 7, 14) else 7
        from bot.db.pg import fetch as pg_fetch

        rows = await pg_fetch("""
            SELECT id, url, lat, lon, price, rooms, area, address, complex_name,
                   views_count, first_seen
            FROM apartment_listings
            WHERE is_active IS NOT FALSE
              AND COALESCE(is_duplicate, FALSE) = FALSE
              AND lat IS NOT NULL AND lon IS NOT NULL
              AND views_count IS NOT NULL
              AND first_seen > now() - ($1 || ' days')::interval
            ORDER BY views_count DESC
            LIMIT 5000
        """, str(days))
        pts = [{
            "id": r["id"], "url": r["url"] or "",
            "lat": float(r["lat"]), "lon": float(r["lon"]),
            "price": r["price"], "rooms": r["rooms"],
            "area": float(r["area"]) if r["area"] else None,
            "address": r["address"] or "", "complex_name": r["complex_name"] or "",
            "views": r["views_count"],
            "first_seen": r["first_seen"].strftime("%d.%m.%Y") if r["first_seen"] else None,
        } for r in rows]
        return JSONResponse({"points": pts, "days": days})

    @router.get("/admin/api/views-coverage-history")
    async def views_coverage_history(request: Request, days: int = 30):
        """Снимки views_coverage_history (пишутся раз в цикл парсера продаж,
        см. service_apartments.py) — сколько активных объявлений всего и
        сколько из них имеют известный views_count, во времени. Раньше этот
        путь по ошибке был занят другим (несовместимым по форме ответа)
        обработчиком, из-за чего уже существующий фронтенд-блок ниже на
        странице получал не те поля и не рисовался."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        days = max(1, min(days, 365))
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT at, total_active, with_views
            FROM views_coverage_history
            WHERE at > now() - ($1 || ' days')::interval
            ORDER BY at ASC
        """, str(days))
        return JSONResponse({"points": [{
            "at": r["at"].strftime("%d.%m %H:%M"),
            "total_active": r["total_active"],
            "with_views": r["with_views"],
        } for r in rows]})

    # ── Объявления без привязки к ЖК ──────────────────────────────────────

    # ── Аналитика: этаж vs скорость ухода в архив / просмотры ──────────────

    @router.get("/admin/api/floor-performance-data")
    async def floor_performance_data(request: Request):
        """Этаж vs скорость ухода в архив (прокси продажи) и просмотры.
        floor_position нет отдельной колонкой в БД (считается на лету при
        парсинге, но никуда не сохраняется) — выводим её тем же правилом
        прямо в SQL: floor=1 -> первый, floor=floors_total -> последний,
        иначе средний."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch

        by_position = await pg_fetch("""
            SELECT
                CASE WHEN floor = 1 THEN 'первый'
                     WHEN floor = floors_total THEN 'последний'
                     ELSE 'средний' END AS position,
                COUNT(*) FILTER (WHERE archived_at IS NOT NULL) AS archived_cnt,
                AVG(EXTRACT(EPOCH FROM (archived_at - first_seen)) / 86400)
                    FILTER (WHERE archived_at IS NOT NULL) AS avg_days_to_archive,
                COUNT(*) FILTER (WHERE is_active IS NOT FALSE AND views_count IS NOT NULL) AS views_cnt,
                AVG(views_count) FILTER (WHERE is_active IS NOT FALSE AND views_count IS NOT NULL) AS avg_views
            FROM apartment_listings
            WHERE floor IS NOT NULL AND floors_total IS NOT NULL
              AND COALESCE(is_duplicate, FALSE) = FALSE
            GROUP BY 1
        """)

        by_floor = await pg_fetch("""
            SELECT
                LEAST(floor, 20) AS floor_bucket,
                COUNT(*) FILTER (WHERE archived_at IS NOT NULL) AS archived_cnt,
                AVG(EXTRACT(EPOCH FROM (archived_at - first_seen)) / 86400)
                    FILTER (WHERE archived_at IS NOT NULL) AS avg_days_to_archive,
                COUNT(*) FILTER (WHERE is_active IS NOT FALSE AND views_count IS NOT NULL) AS views_cnt,
                AVG(views_count) FILTER (WHERE is_active IS NOT FALSE AND views_count IS NOT NULL) AS avg_views
            FROM apartment_listings
            WHERE floor IS NOT NULL AND floor > 0
              AND COALESCE(is_duplicate, FALSE) = FALSE
            GROUP BY 1
            ORDER BY 1
        """)

        def _fmt(rows, key):
            return [{
                key: r[key] if key != "floor_bucket" else (str(r[key]) if r[key] < 20 else "20+"),
                "archived_cnt": r["archived_cnt"] or 0,
                "avg_days_to_archive": round(float(r["avg_days_to_archive"]), 1) if r["avg_days_to_archive"] is not None else None,
                "views_cnt": r["views_cnt"] or 0,
                "avg_views": round(float(r["avg_views"]), 0) if r["avg_views"] is not None else None,
            } for r in rows]

        return JSONResponse({
            "by_position": _fmt(by_position, "position"),
            "by_floor": _fmt(by_floor, "floor_bucket"),
        })

    @router.get("/admin/api/parser-cycle-history")
    async def parser_cycle_history(request: Request, days: int = 14):
        """Снимки parser_cycle_history — сколько времени занимает цикл
        парсера, сколько реальных HTTP-запросов к Крыше он делает
        (search+detail), и (см. задачу "оптимизация работы парсеров" —
        пропуск detail-fetch для объявлений без изменений цены/координат)
        эффективность этой оптимизации за цикл: total_seen/needs_detail_fetch/
        skipped_no_change + skip_rate_pct. Для графиков на
        /admin/parsers?tab=recheck (секция "Нагрузка на Крышу").
        cumulative_time_saved_sec — оценка суммарного сэкономленного времени
        за ВСЮ историю (не ограничена days): каждый пропущенный
        detail-fetch экономит ~11.5с (середина паузы 8-15с между запросами
        деталей, см. apartment_parser.analyze_apartments)."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch, fetchval as pg_fetchval
        rows = await pg_fetch("""
            SELECT at, duration_sec, search_requests, detail_requests,
                   total_seen, needs_detail_fetch, skipped_no_change
            FROM parser_cycle_history
            WHERE at > now() - ($1 || ' days')::interval
            ORDER BY at ASC
        """, str(days))
        AVG_DETAIL_DELAY_SEC = 11.5
        cum_skipped = await pg_fetchval(
            "SELECT COALESCE(SUM(skipped_no_change), 0) FROM parser_cycle_history") or 0
        points = []
        for r in rows:
            total_seen = r["total_seen"]
            skipped = r["skipped_no_change"]
            needs = r["needs_detail_fetch"]
            skip_rate = round(100.0 * skipped / total_seen, 1) if total_seen else None
            points.append({
                "at": r["at"].strftime("%d.%m %H:%M"),
                "duration_min": round((r["duration_sec"] or 0) / 60, 1),
                "search_requests": r["search_requests"] or 0,
                "detail_requests": r["detail_requests"] or 0,
                "total_requests": (r["search_requests"] or 0) + (r["detail_requests"] or 0),
                "total_seen": total_seen,
                "needs_detail_fetch": needs,
                "skipped_no_change": skipped,
                "skip_rate_pct": skip_rate,
            })
        return JSONResponse({
            "points": points,
            "cumulative_time_saved_sec": round(cum_skipped * AVG_DETAIL_DELAY_SEC),
            "avg_detail_delay_sec": AVG_DETAIL_DELAY_SEC,
        })

    @router.get("/admin/api/score-confidence-points")
    async def score_confidence_points(request: Request, rooms: str = ""):
        """Точки для гекс-карты уверенности Смарт рейтинга на /admin/analytics —
        confidence (0-100%, см. bot/core/deal_score.py) по каждому активному
        объявлению с координатами, опционально по комнатности."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        conds = ["a.lat IS NOT NULL", "a.lon IS NOT NULL", "a.is_active IS NOT FALSE",
                 "COALESCE(a.is_duplicate, FALSE) = FALSE", "a.deal_confidence IS NOT NULL"]
        params = []
        if rooms:
            conds.append(f"a.rooms = ${len(params)+1}")
            params.append(int(rooms))
        rows = await pg_fetch(f"""
            SELECT a.lat, a.lon, a.deal_confidence, a.score_total, a.rooms
            FROM apartment_listings a
            WHERE {' AND '.join(conds)}
            LIMIT 20000
        """, *params)
        return JSONResponse({"points": [{
            "lat": float(r["lat"]), "lon": float(r["lon"]),
            "confidence": r["deal_confidence"], "score": r["score_total"], "rooms": r["rooms"],
        } for r in rows]})

    @router.get("/admin/api/ceiling-history")
    async def ceiling_history(request: Request, days: int = 30):
        """Снимки ceiling_stats_history — доля активных объявлений с
        известной высотой потолка во времени (см. /admin/analytics/ceiling).
        Потолок, как и этаж, приходит только с детальной страницы."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT at, total_active, with_ceiling
            FROM ceiling_stats_history
            WHERE at > now() - ($1 || ' days')::interval
            ORDER BY at ASC
        """, str(days))
        return JSONResponse({"points": [{
            "at": r["at"].strftime("%d.%m %H:%M"),
            "total_active": r["total_active"],
            "with_ceiling": r["with_ceiling"],
            "pct": round(100 * r["with_ceiling"] / r["total_active"], 1) if r["total_active"] else 0,
        } for r in rows]})

    @router.get("/admin/api/floor-sold-counts")
    async def floor_sold_counts(request: Request, period: str = "month"):
        """Этаж vs количество проданных (ушедших в архив) квартир за период —
        для графика на /admin/analytics/floor-performance. \"Продано\" тут —
        та же прокси, что и в floor-performance-data: уход в архив
        (archived_at), не гарантированный факт продажи."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch

        intervals = {
            "today": "archived_at::date = CURRENT_DATE",
            "3d": "archived_at > now() - interval '3 days'",
            "7d": "archived_at > now() - interval '7 days'",
            "month": "archived_at > now() - interval '30 days'",
            "3m": "archived_at > now() - interval '90 days'",
            "6m": "archived_at > now() - interval '180 days'",
        }
        cond = intervals.get(period, intervals["month"])

        rows = await pg_fetch(f"""
            SELECT LEAST(floor, 20) AS floor_bucket, COUNT(*) AS sold_cnt
            FROM apartment_listings
            WHERE floor IS NOT NULL AND floor > 0
              AND archived_at IS NOT NULL AND {cond}
              AND COALESCE(is_duplicate, FALSE) = FALSE
            GROUP BY 1
            ORDER BY 1
        """)
        points = [{
            "floor": str(r["floor_bucket"]) if r["floor_bucket"] < 20 else "20+",
            "sold_cnt": r["sold_cnt"] or 0,
        } for r in rows]
        return JSONResponse({"points": points, "period": period})

    @router.get("/admin/unbound", response_class=HTMLResponse)
    async def unbound_page(request: Request):
        # Слито в /admin/analytics/floors (этажи+потолок+этаж-vs-продажи+
        # координаты одной страницей) — API-ручки unbound-* ниже остались,
        # их использует JS на объединённой странице.
        return RedirectResponse(url="/admin/analytics/floors", status_code=301)

    @router.get("/admin/api/unbound-points")
    async def unbound_points(request: Request):
        """Активные объявления без ЖК И без точных координат — те, что
        реально нуждаются во внимании. Объявления, у которых уже ЕСТЬ точные
        координаты (просто ЖК не извлёкся), сюда больше не попадают: они и
        так полностью видны на главной карте, дублировать их здесь незачем
        (раньше показывались с оранжевой меткой "точно" — было наглядно
        задвоение с дашбордом)."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT id, url, title, price, rooms, area, address, district, first_seen
            FROM apartment_listings
            WHERE is_active IS NOT FALSE
              AND COALESCE(is_duplicate, FALSE) = FALSE
              AND (complex_name IS NULL OR btrim(complex_name) = '')
              AND lat IS NULL
            ORDER BY first_seen DESC LIMIT 3000
        """)
        district_geo = {r2["district"]: (float(r2["lat"]), float(r2["lon"]))
                        for r2 in await pg_fetch("""
            SELECT district, AVG(lat) AS lat, AVG(lon) AS lon
            FROM apartment_listings
            WHERE lat IS NOT NULL AND district IS NOT NULL AND district != ''
            GROUP BY district""")}
        import random as _rnd
        pts, no_geo = [], 0
        for r in rows:
            d = dict(r)
            dg = district_geo.get(d.get("district") or "")
            if not dg:
                no_geo += 1
                continue
            lat, lon, binding = dg[0], dg[1], "район"
            lat += _rnd.uniform(-0.004, 0.004)
            lon += _rnd.uniform(-0.006, 0.006)
            pts.append({
                "id": d["id"], "url": d["url"] or "",
                "title": d["title"] or "",
                "lat": lat, "lon": lon, "binding": binding,
                "price": d["price"], "rooms": d["rooms"],
                "area": float(d["area"] or 0),
                "address": d["address"] or "", "district": d["district"] or "",
                "found": d["first_seen"].strftime("%d.%m.%Y") if d["first_seen"] else "",
            })
        return JSONResponse({"points": pts, "count": len(pts), "no_geo": no_geo})

    @router.get("/admin/api/unbound-no-coords")
    async def unbound_no_coords(request: Request):
        """Объявления совсем без своих координат (lat IS NULL) — список для
        ручной простановки локации на карте, а не только строгий счётчик
        no_geo (тот требует ЕЩЁ и отсутствия геоцентроида по району)."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT id, url, title, price, rooms, area, address, district, first_seen
            FROM apartment_listings
            WHERE is_active IS NOT FALSE
              AND COALESCE(is_duplicate, FALSE) = FALSE
              AND lat IS NULL
            ORDER BY first_seen DESC LIMIT 200
        """)
        return JSONResponse({"points": [{
            "id": r["id"], "url": r["url"] or "",
            "title": r["title"] or "",
            "price": r["price"], "rooms": r["rooms"],
            "area": float(r["area"]) if r["area"] else None,
            "address": r["address"] or "", "district": r["district"] or "",
            "found": r["first_seen"].strftime("%d.%m.%Y") if r["first_seen"] else "",
        } for r in rows]})

    # ── Привязка к ЖК: фоновая задача (не блокирует веб) + поллинг статуса ──

    rebind_state = {"running": False, "stage": "", "result": None}

    async def _do_rebind() -> dict:
        from bot.core.rebind import run_rebind, record_unbound_snapshot

        def _set_stage(stage: str) -> None:
            rebind_state["stage"] = stage

        result = await run_rebind(progress_cb=_set_stage)
        # Снимок для графика на /admin/unbound — сразу после ручного запуска,
        # чтобы точка на графике отражала реальный эффект нажатия кнопки.
        await record_unbound_snapshot()
        return result

    @router.get("/admin/api/hex-prices")
    async def hex_prices_api(request: Request, lat: float, lon: float, rooms: int = 0):
        """Цена/м² по гексагону вокруг точки + 6 соседей (продажа/аренда) —
        для мини-карты в попапе объявления на дашборде. rooms (опц.) — аренда
        считается по той же комнатности, что и у самого объявления."""
        sale, rental = await hex_price_cells(lat, lon, rooms or None)
        return JSONResponse({"sale": sale, "rental": rental})

    @router.get("/admin/api/unbound-history")
    async def unbound_history(request: Request):
        """История снимков unbound_stats_history — для графика на /admin/unbound."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT at, total_active, unbound, unbound_coords
            FROM unbound_stats_history
            ORDER BY at ASC
        """)
        return JSONResponse({"points": [{
            "at": r["at"].strftime("%d.%m %H:%M"),
            "total_active": r["total_active"],
            "unbound": r["unbound"],
            "unbound_coords": r["unbound_coords"],
        } for r in rows]})

    @router.get("/admin/rebind/status")
    async def rebind_status(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        return JSONResponse(rebind_state)

    @router.post("/admin/rebind")
    async def rebind_listings(request: Request):
        """Запуск привязки в ФОНЕ — ответ мгновенный, прогресс через
        GET /admin/rebind/status. Идемпотентно, можно запускать повторно."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        if rebind_state["running"]:
            return JSONResponse({"ok": True, "already_running": True,
                                 "stage": rebind_state["stage"]})
        import asyncio as _aio
        rebind_state.update(running=True, stage="запуск…", result=None)

        async def _runner():
            try:
                rebind_state["result"] = await _do_rebind()
                rebind_state["stage"] = "готово"
            except Exception as e:
                logger.exception("rebind failed")
                rebind_state["stage"] = f"ошибка: {e}"
            finally:
                rebind_state["running"] = False

        _aio.create_task(_runner())
        return JSONResponse({"ok": True, "started": True})

    # ── Аудит «ЖК-улиц» ───────────────────────────────────────────────────

    @router.get("/admin/complexes/audit")
    async def complexes_audit(request: Request):
        """Превью: какие 'ЖК' на самом деле улицы (название в адресах ≥60% объявлений)."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.core.complex_audit import audit_complexes
        suspects = await audit_complexes()
        return JSONResponse({"suspects": suspects, "count": len(suspects)})

    @router.post("/admin/complexes/audit/apply")
    async def complexes_audit_apply(request: Request):
        """Применить: пометить псевдо-ЖК улицами и отвязать их объявления."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.core.complex_audit import purge_street_complexes
        res = await purge_street_complexes()
        return JSONResponse({"ok": True, **res})

    return router
