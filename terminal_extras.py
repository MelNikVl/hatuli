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
        if archive_rows:
            for r in archive_rows:
                hid = _hex_id(float(r["lat"]), float(r["lon"]), HEX_EDGE)
                archive_buckets.setdefault(hid, []).append(float(r["price"]) / float(r["area"]))
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
          AND archived_at > now() - interval '180 days'
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
          AND last_seen > now() - interval '30 days'
          {rental_room_cond}
    """, *rental_params)
    return _build(nearby_sale, archive_rows=nearby_sale_archived), _build(nearby_rental, with_total=True)


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
_PHOTO_ALLOWED_HOSTS = ("kcdn.online", "krisha.kz")
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


def make_extras_router(templates) -> APIRouter:
    router = APIRouter()

    def is_authed(request: Request) -> bool:
        return request.cookies.get("admin_auth") == "1"

    # Доступно во всех шаблонах: {{ is_admin(request) }} — для скрытия
    # админ-элементов на публичных страницах
    templates.env.globals["is_admin"] = is_authed

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
        from bot.core.site_auth import get_user_by_session, list_favorites
        user = await get_user_by_session(_site_session_cookie(request))
        favorites = await list_favorites(user["user_id"]) if user else []
        return templates.TemplateResponse("cabinet.html", {
            "request": request, "user": user, "favorites": favorites,
            "bot_username": os.getenv("SITE_BOT_USERNAME", "nik_us_bot"),
        })

    @router.get("/favorites", response_class=HTMLResponse)
    async def favorites_page(request: Request):
        """Отдельная страница избранного (карточки + сравнение таблицей) —
        раньше избранное было видно только внутри /cabinet одним списком."""
        from bot.core.site_auth import get_user_by_session, list_favorites
        user = await get_user_by_session(_site_session_cookie(request))
        favorites = await list_favorites(user["user_id"]) if user else []
        return templates.TemplateResponse("favorites.html", {
            "request": request, "user": user, "favorites": favorites,
        })

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
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", "--quiet", f"{service}.service",
        )
        await proc.wait()
        return proc.returncode == 0

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
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        await app_settings.load()
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
        })

    # ── API: точки для карты на дашборде ─────────────────────────────────

    # ── Детализация парсера: один график продажи+аренда, разными цветами ───

    @router.get("/admin/parser/sales")
    async def parser_sales_redirect(days: int = 1):
        return RedirectResponse(url=f"/admin/parser?days={days}", status_code=301)

    @router.get("/admin/parser/rental")
    async def parser_rental_redirect(days: int = 1):
        return RedirectResponse(url=f"/admin/parser?days={days}", status_code=301)

    @router.get("/admin/parser", response_class=HTMLResponse)
    async def parser_combined(request: Request, days: int = 1):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        days = days if days in (1, 3, 5) else 1
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

        # Время полного обхода Крыши (см. service_apartments.py: DEEP_SWEEP_*) —
        # длина одного круга глубокого обхода = время, за которое сверяются
        # все объявления в базе + парсятся новые.
        from bot.db import settings as app_settings
        await app_settings.load()
        full_cycle_sec = app_settings.get_int("DEEP_SWEEP_CIRCLE_DURATION_SEC", 0)
        full_cycle_completed_at = app_settings.get("DEEP_SWEEP_CIRCLE_COMPLETED_AT", "")
        full_cycle_hours = round(full_cycle_sec / 3600, 1) if full_cycle_sec else None

        # Просмотры (микросервис krisha-viewcount, Playwright)
        viewcount_total = await pg_fetchval(
            "SELECT COUNT(*) FROM apartment_listings WHERE views_count IS NOT NULL") or 0
        viewcount_fresh = await pg_fetchval(
            "SELECT COUNT(*) FROM apartment_listings WHERE views_count_updated_at > now() - interval '6 hours'") or 0
        viewcount_last_at = await pg_fetchval(
            "SELECT MAX(views_count_updated_at) FROM apartment_listings")

        # Частота пересчёта топ-10 по скору (Deal Score v3, apply_deal_scores)
        top10_recalc_at = app_settings.get("DEAL_SCORE_LAST_RUN_AT", "")

        stats = [
            {"label": "продажа: активных в мониторинге", "value": f"{total_active:,}".replace(",", " ")},
            {"label": "продажа: спаршено сегодня", "value": today_new_sale},
            {"label": "продажа: ушло в архив сегодня", "value": today_archived, "color": "#f59e0b"},
            {"label": "продажа: цена ↓ сегодня", "value": price_down, "color": "#16a34a"},
            {"label": "продажа: цена ↑ сегодня", "value": price_up, "color": "#ef4444"},
            {"label": "аренда: живых (видели за 3 дня)", "value": f"{rental_fresh:,}".replace(",", " ")},
            {"label": "аренда: всего в базе", "value": f"{rental_total:,}".replace(",", " ")},
            {"label": "аренда: спаршено сегодня", "value": today_new_rental},
            {"label": "полный обход Крыши: последний круг", "value": f"{full_cycle_hours} ч" if full_cycle_hours else "считается…"},
            {"label": "просмотры: покрыто объявлений", "value": f"{viewcount_total:,}".replace(",", " ")},
            {"label": "просмотры: обновлено за 6 ч", "value": f"{viewcount_fresh:,}".replace(",", " ")},
        ]
        return templates.TemplateResponse("parser_detail.html", {
            "request": request, "title": "🕷 Парсер — продажа и аренда",
            "atab": "rental",
            "days": days, "stats": stats,
            "chart_labels": labels,
            "sale_values": sale_values, "rental_values": rental_values,
            "full_cycle_hours": full_cycle_hours,
            "full_cycle_completed_at": full_cycle_completed_at,
            "viewcount_total": viewcount_total,
            "viewcount_fresh": viewcount_fresh,
            "viewcount_last_at": viewcount_last_at.strftime("%d.%m %H:%M") if viewcount_last_at else None,
            "top10_recalc_at": top10_recalc_at,
        })

    @router.get("/admin/duplicates", response_class=HTMLResponse)
    async def duplicates_page(request: Request):
        """Страница дублей: кто чей дубль, со ссылками."""
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import fetch as pg_fetch, execute as pg_exec2
        # колонка появляется после первого прогона дедупа — создаём сами,
        # чтобы страница не падала на свежей базе
        await pg_exec2("ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS dup_match TEXT")
        rows = await pg_fetch("""
            SELECT p.id, p.address, p.price, p.rooms, p.area, p.is_owner,
                   p.seller_name, p.lat, p.lon, p.complex_name, p.floor, p.floors_total,
                   p.photos AS p_photos, COUNT(d.id) AS dup_cnt,
                   json_agg(json_build_object(
                       'id', d.id, 'price', d.price, 'is_owner', d.is_owner,
                       'url', d.url, 'match', COALESCE(d.dup_match, '?'),
                       'seller_name', d.seller_name, 'floor', d.floor,
                       'floors_total', d.floors_total, 'photos', d.photos,
                       'rooms', d.rooms, 'area', d.area
                       ) ORDER BY d.is_owner DESC NULLS LAST, d.price ASC) AS dups
            FROM apartment_listings p
            JOIN apartment_listings d ON d.duplicate_of = p.id AND d.is_duplicate = TRUE
            GROUP BY p.id
            ORDER BY dup_cnt DESC, p.last_seen DESC NULLS LAST
            LIMIT 300
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
        rows = await pg_fetch(
            "SELECT kind, name, lat, lon, address FROM city_poi LIMIT 3000")
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
            LIMIT 2500
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

    @router.get("/admin/api/complexes-map")
    async def complexes_map(request: Request):
        """Все ЖК с координатами (центроид объявлений) для карты рейтинга."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
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
            LIMIT 2500
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
                              seller: str = "", market: str = ""):
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
            conds.append(f"AND rooms = ${i}"); params.append(int(rooms)); i += 1
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
                         offset: int = 0, limit: int = 15000,
                         min_lat: float = 0, max_lat: float = 0,
                         min_lon: float = 0, max_lon: float = 0):
        # публичный (карта на главной без логина); coverage — только админу
        from bot.db.pg import fetch as pg_fetch, fetchval as pg_fetchval2

        if type == "rental":
            # У аренды нет своих координат — привязываем к центроиду ЖК
            # (по объявлениям продажи того же ЖК). Позиция приблизительная.
            conds, params, i = ["1=1"], [], 1
            if rooms:
                conds.append(f"r.rooms = ${i}"); params.append(int(rooms)); i += 1
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
                       r.lat AS own_lat, r.lon AS own_lon,
                       ph.old_price AS prev_price, ph.changed_at AS price_changed_at
                FROM rental_listings r
                LEFT JOIN LATERAL (
                    SELECT old_price, changed_at FROM rental_price_history h
                    WHERE h.listing_id = r.id
                    ORDER BY changed_at DESC LIMIT 1
                ) ph ON TRUE
                WHERE {' AND '.join(conds)}
                  AND r.last_seen > now() - interval '14 days'
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
        if rooms:
            conds.append(f"AND rooms = ${i}"); params.append(int(rooms)); i += 1
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
                   a.description, a.ceiling_height, a.finish_type, a.floor, a.floors_total,
                   a.score_yield, a.score_price_market, a.score_location,
                   a.score_apt_type, a.score_floor, a.score_complex, a.score_supply,
                   EXTRACT(EPOCH FROM (now() - a.first_seen))/86400 AS age_days,
                   (CASE WHEN a.market_type = 'primary' AND a.primary_score_total IS NOT NULL
                         THEN a.primary_score_total
                         ELSE COALESCE(a.score_total,0) END
                    + COALESCE(a.zone_bonus,0)
                    + COALESCE(a.layer_bonus,0)
                    + COALESCE(a.price_drop_bonus,0)) AS eff_score,
                   ph.old_price AS prev_price,
                   ph.changed_at AS price_changed_at,
                   dv.id AS developer_id, dv.name AS developer_name
            FROM apartment_listings a
            LEFT JOIN LATERAL (
                SELECT old_price, changed_at FROM price_history h
                WHERE h.listing_id = a.id
                ORDER BY changed_at DESC LIMIT 1
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
            "year_built": r["year_built"],
            "description": r["description"] or "",
            "ceiling_height": float(r["ceiling_height"]) if r["ceiling_height"] is not None else None,
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

    @router.get("/admin/api/archived-sale-points")
    async def archived_sale_points(request: Request):
        """Последняя цена перед уходом в архив (за последние 180 дней) — для
        теплокарты продаж на дашборде: гексагоны без активных объявлений
        сейчас не обязаны быть пустыми, если там недавно что-то продалось."""
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT id, lat, lon, price, rooms
            FROM apartment_listings
            WHERE is_active = FALSE AND archived_at IS NOT NULL
              AND COALESCE(is_duplicate, FALSE) = FALSE
              AND lat IS NOT NULL AND lon IS NOT NULL
              AND price > 500000
              AND archived_at > now() - interval '180 days'
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
                   c.housing_class, c.year_built, c.korter_url,
                   COUNT(a.id) FILTER (WHERE a.is_active IS NOT FALSE
                       AND COALESCE(a.is_duplicate, FALSE) = FALSE) AS active_cnt
            FROM complexes c
            LEFT JOIN developers d ON d.id = c.developer_id
            LEFT JOIN apartment_listings a ON lower(trim(a.complex_name)) = lower(trim(c.name))
            WHERE COALESCE(c.is_street, FALSE) = FALSE
            GROUP BY c.id, d.name
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
            })
        total_cx = await fetch("SELECT COUNT(*) AS n FROM complexes WHERE COALESCE(is_street, FALSE) = FALSE")
        return templates.TemplateResponse("complexes_audit.html", {
            "request": request, "rows": out, "limit": limit,
            "total_cx": total_cx[0]["n"] if total_cx else 0,
        })

    @router.get("/admin/complex/{complex_id}", response_class=HTMLResponse)
    async def complex_detail(request: Request, complex_id: int):
        # Публичная страница — админ-элементы (редактирование фото/контактов)
        # скрываются в шаблоне через is_admin(request)
        from bot.db.pg import fetchrow
        cx = await fetchrow("""
            SELECT c.*, d.name AS developer_name
            FROM complexes c LEFT JOIN developers d ON d.id = c.developer_id
            WHERE c.id = $1
        """, complex_id)
        if not cx:
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

        # Сравнение цены/м² по гексагону ЖК и 6 соседним (микролокация),
        # отдельно для продажи и аренды — общая логика вынесена в hex_price_cells()
        # (переиспользуется и мини-картой в попапе объявления на дашборде).
        hex_cells_sale, hex_cells_rental = [], []
        if geo and geo["lat"]:
            hex_cells_sale, hex_cells_rental = await hex_price_cells(
                float(geo["lat"]), float(geo["lon"]))

        hex_cells = hex_cells_sale  # обратная совместимость, если где-то ещё используется

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

        # Все объявления ЖК с координатами — для карты на странице ЖК
        cx_map_points = await fetch("""
            SELECT id, lat, lon, price, rooms, area, is_active, url,
                   COALESCE(is_duplicate, FALSE) AS is_dup
            FROM apartment_listings
            WHERE lower(trim(complex_name)) = lower(trim($1))
              AND lat IS NOT NULL
        """, cname)
        cx_total = await fetchrow("""
            SELECT COUNT(*) FILTER (WHERE is_active IS NOT FALSE
                                    AND COALESCE(is_duplicate, FALSE) = FALSE) AS live,
                   COUNT(*) FILTER (WHERE is_active IS NOT FALSE
                                    AND COALESCE(is_duplicate, FALSE) = FALSE
                                    AND lat IS NULL) AS live_no_coords
            FROM apartment_listings
            WHERE lower(trim(complex_name)) = lower(trim($1))
        """, cname)

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

        return templates.TemplateResponse("complex_detail.html", {
            "request": request,
            "cx": dict(cx),
            "cx_sources": cx_sources,
            "geo": {"lat": float(geo["lat"]), "lon": float(geo["lon"])} if geo and geo["lat"] else None,
            "cx_address": addr_row["address"] if addr_row else None,
            "developer": developer,
            "sales": [dict(r) for r in sale_listings],
            "rentals": [dict(r) for r in rentals],
            "stats": [dict(r) for r in stats],
            "rental_stats": [dict(r) for r in rental_stats],
            "obs": dict(obs) if obs else {},
            "pace": dict(pace) if pace else {},
            "map_points": [dict(r) for r in cx_map_points],
            "live_cnt": (cx_total["live"] or 0) if cx_total else 0,
            "live_no_coords": (cx_total["live_no_coords"] or 0) if cx_total else 0,
            "hex_cells": hex_cells,
            "hex_cells_sale": hex_cells_sale,
            "hex_cells_rental": hex_cells_rental,
        })

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

    # ── Застройщики: список и карточка ────────────────────────────────────

    @router.get("/admin/developers", response_class=HTMLResponse)
    async def developers_page(request: Request, min_active: int = 0):
        """Таблица по всем застройщикам: сколько объектов и какие данные о
        них известны (тот же формат, что и Аудит данных по ЖК) — фильтр по
        числу активных объявлений, название ссылкой на карточку."""
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT d.id, d.name, d.founded_year, d.website, d.description,
                   d.projects_active, d.projects_total, d.projects_delivered,
                   d.projects_delayed, d.avg_delay_months, d.has_court_cases,
                   d.court_cases_count, d.score_total, d.homsters_slug,
                   COUNT(c.id) AS cx_cnt,
                   COALESCE(SUM(c.listings_count), 0) AS active_cnt,
                   COALESCE(SUM(c.sold_count), 0) AS sold_cnt
            FROM developers d
            LEFT JOIN complexes c ON c.developer_id = d.id
                                 AND COALESCE(c.is_street, FALSE) = FALSE
            GROUP BY d.id
            ORDER BY active_cnt DESC, cx_cnt DESC, d.name
        """)
        out = []
        for r in rows:
            d = dict(r)
            if d["active_cnt"] < min_active:
                continue
            out.append({
                **d,
                "has_founded": d["founded_year"] is not None,
                "has_website": bool(d["website"]),
                "has_description": bool(d["description"]),
                "has_score": d["score_total"] is not None,
                "has_homsters": bool(d["homsters_slug"]),
                "has_delay_data": d["avg_delay_months"] is not None,
            })
        return templates.TemplateResponse("developers.html", {
            "request": request,
            "developers": out,
            "total": len(out),
            "min_active": min_active,
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
        return templates.TemplateResponse("developer_detail.html", {
            "request": request,
            "dev": dict(dev),
            "complexes": [dict(r) for r in complexes],
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

        from bot.core.listing_intel import build_negotiation_points, build_seller_questions
        negotiation_points = build_negotiation_points(l, bargain, len(comps))
        seller_questions = build_seller_questions(l)

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
            "description": l.get("description") or "",
            "first_seen": l["first_seen"].strftime("%d.%m.%Y") if l.get("first_seen") else None,
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
            "payback_years": l.get("payback_years"),
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
        парсера и сколько реальных HTTP-запросов к Крыше он делает
        (search+detail) — для графиков на /admin/parser."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT at, duration_sec, search_requests, detail_requests
            FROM parser_cycle_history
            WHERE at > now() - ($1 || ' days')::interval
            ORDER BY at ASC
        """, str(days))
        return JSONResponse({"points": [{
            "at": r["at"].strftime("%d.%m %H:%M"),
            "duration_min": round((r["duration_sec"] or 0) / 60, 1),
            "search_requests": r["search_requests"] or 0,
            "detail_requests": r["detail_requests"] or 0,
            "total_requests": (r["search_requests"] or 0) + (r["detail_requests"] or 0),
        } for r in rows]})

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
            "confidence": r["deal_confidence"], "score": r["score_total"],
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
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import fetchval as pg_fv
        stats = {
            "total_active": await pg_fv(
                "SELECT COUNT(*) FROM apartment_listings "
                "WHERE is_active IS NOT FALSE "
                "AND COALESCE(is_duplicate, FALSE) = FALSE") or 0,
            "unbound": await pg_fv(
                "SELECT COUNT(*) FROM apartment_listings "
                "WHERE is_active IS NOT FALSE "
                "AND COALESCE(is_duplicate, FALSE) = FALSE "
                "AND (complex_name IS NULL OR btrim(complex_name) = '')") or 0,
            "unbound_coords": await pg_fv(
                "SELECT COUNT(*) FROM apartment_listings "
                "WHERE is_active IS NOT FALSE "
                "AND COALESCE(is_duplicate, FALSE) = FALSE "
                "AND (complex_name IS NULL OR btrim(complex_name) = '') "
                "AND lat IS NOT NULL") or 0,
        }
        return templates.TemplateResponse("unbound.html", {
            "request": request, "atab": "unbound", "stats": stats,
        })

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
