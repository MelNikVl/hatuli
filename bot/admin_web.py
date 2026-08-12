"""
Admin web panel (FastAPI + Jinja2).

Migrated from krisha_bot/admin_web.py — updated to use bot.db.compat.BotDB
and templates located in bot/templates/.
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import bot.state as _state
from bot.db.compat import BotDB

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
_LOG_FILE = "web.log"
# Свои загруженные фото (ЖК/застройщики) — раздаются напрямую с диска сервера,
# без внешних URL. Каталог создаётся при первом запуске, если его ещё нет.
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
_UPLOADS_DIR = os.path.join(_STATIC_DIR, "uploads")
os.makedirs(os.path.join(_UPLOADS_DIR, "complexes"), exist_ok=True)
os.makedirs(os.path.join(_UPLOADS_DIR, "developers"), exist_ok=True)


def create_admin_app(db: BotDB, admin_password: str, bot_version: str, db_path: str = "") -> FastAPI:
    app = FastAPI(title="Krisha Bot Admin")
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)

    def is_authed(request: Request) -> bool:
        return request.cookies.get("admin_auth") == "1"

    # ── SEO/AI-краулеры: robots.txt/sitemap.xml/llms.txt на самом ────────────
    # приложении. Cloudflare перед ним отдаёт свой собственный managed
    # robots.txt (Content-Signal блок), который блокирует GPTBot/ClaudeBot/
    # Google-Extended/CCBot и др. по умолчанию — эти файлы на origin ничего
    # не решают, пока в Cloudflare (Security → Bots → AI Bots / Content
    # Signals) не разрешат нужных ботов вручную. См. заметку в чате.
    from fastapi.responses import PlainTextResponse, Response as _Response

    @app.get("/robots.txt", response_class=PlainTextResponse)
    async def robots_txt():
        return PlainTextResponse(
            "User-agent: *\n"
            "Allow: /\n\n"
            "User-agent: GPTBot\nAllow: /\n\n"
            "User-agent: OAI-SearchBot\nAllow: /\n\n"
            "User-agent: ChatGPT-User\nAllow: /\n\n"
            "User-agent: ClaudeBot\nAllow: /\n\n"
            "User-agent: Claude-SearchBot\nAllow: /\n\n"
            "User-agent: Claude-User\nAllow: /\n\n"
            "User-agent: PerplexityBot\nAllow: /\n\n"
            "User-agent: Google-Extended\nAllow: /\n\n"
            "User-agent: Applebot-Extended\nAllow: /\n\n"
            "User-agent: Yandex\nAllow: /\n\n"
            "Disallow: /admin/login\n"
            "Disallow: /admin/api/\n"
            "Disallow: /cabinet\n\n"
            "Sitemap: https://hatuli.ai-groundtruth.com/sitemap.xml\n"
        )

    @app.get("/llms.txt", response_class=PlainTextResponse)
    async def llms_txt():
        return PlainTextResponse(
            "# Hatuli\n\n"
            "> Карта квартир Астаны: продажа и аренда, тепловые карты цен/шума/"
            "транспортной доступности, рейтинг жилых комплексов, оценка справедливой "
            "цены и торга по объявлению.\n\n"
            "## Ключевые страницы\n"
            "- [Карта квартир](https://hatuli.ai-groundtruth.com/admin): все активные объявления продажи и аренды в Астане с фильтрами и тепловыми картами\n"
            "- [Жилые комплексы](https://hatuli.ai-groundtruth.com/admin/complexes): рейтинг ЖК Астаны по цене, застройщику, инфраструктуре\n"
            "- [Застройщики](https://hatuli.ai-groundtruth.com/admin/developers): список застройщиков и их проектов\n"
            "- [Инфо](https://hatuli.ai-groundtruth.com/admin/info): методология скоринга и расчётов\n"
        )

    @app.get("/sitemap.xml")
    async def sitemap_xml():
        from bot.db.pg import fetch as pg_fetch
        urls = [
            ("https://hatuli.ai-groundtruth.com/admin", "hourly", "1.0"),
            ("https://hatuli.ai-groundtruth.com/admin/complexes", "daily", "0.9"),
            ("https://hatuli.ai-groundtruth.com/admin/developers", "weekly", "0.6"),
            ("https://hatuli.ai-groundtruth.com/admin/info", "monthly", "0.4"),
        ]
        # Топ-2000 ЖК по активным объявлениям — полный список (2500+) в один
        # sitemap не кладём (мягкий лимит поисковиков — 50k URL, но страницы
        # без единого объявления малоценны для индексации).
        rows = await pg_fetch("""
            SELECT c.id, c.updated_at,
                   COUNT(a.id) FILTER (WHERE a.is_active IS NOT FALSE) AS active_cnt
            FROM complexes c
            LEFT JOIN apartment_listings a ON lower(trim(a.complex_name)) = lower(trim(c.name))
            WHERE COALESCE(c.is_street, FALSE) = FALSE
            GROUP BY c.id
            HAVING COUNT(a.id) FILTER (WHERE a.is_active IS NOT FALSE) > 0
            ORDER BY active_cnt DESC
            LIMIT 2000
        """)
        body = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for loc, freq, prio in urls:
            body.append(f"<url><loc>{loc}</loc><changefreq>{freq}</changefreq><priority>{prio}</priority></url>")
        for r in rows:
            lastmod = r["updated_at"].strftime("%Y-%m-%d") if r["updated_at"] else ""
            body.append(
                f"<url><loc>https://hatuli.ai-groundtruth.com/admin/complex/{r['id']}</loc>"
                f"{'<lastmod>' + lastmod + '</lastmod>' if lastmod else ''}"
                f"<changefreq>weekly</changefreq><priority>0.7</priority></url>"
            )
        body.append("</urlset>")
        return _Response("\n".join(body), media_type="application/xml")

    @app.get("/admin/login", response_class=HTMLResponse)
    async def admin_login_page(request: Request):
        return templates.TemplateResponse("login.html", {"request": request, "error": None})

    @app.post("/admin/login", response_class=HTMLResponse)
    async def admin_login(request: Request, username: str = Form(default="admin"), password: str = Form(...)):
        from bot.core.auth_users import ensure_seeded, get_user, verify_password
        await ensure_seeded(admin_password)
        user = await get_user(username.strip() or "admin")
        if not user or not verify_password(password, user["password_hash"]):
            return templates.TemplateResponse("login.html", {"request": request, "error": "Неверный логин или пароль"})
        response = RedirectResponse(url="/admin", status_code=302)
        response.set_cookie("admin_auth", "1", httponly=True)
        response.set_cookie("admin_user", user["username"], httponly=True)
        return response

    @app.get("/admin/logout")
    async def admin_logout():
        response = RedirectResponse(url="/admin/login", status_code=302)
        response.delete_cookie("admin_auth")
        response.delete_cookie("admin_user")
        return response

    async def _render_dashboard(request: Request, listing_id: str | None = None, listing_meta: dict | None = None):
        # Общий рендер главной карты — вынесено в helper, чтобы шарить между
        # /admin (голая карта) и /admin/listing/{id} (та же карта с открытым
        # попапом объявления, см. задачу "попап объявления = отдельная
        # шарабельная страница, старую /admin/analytics/{id} убрать").
        stats = await db.get_dashboard_stats()
        from bot.db import settings as app_settings
        await app_settings.load()
        # Личный кабинет посетителя (вход через Telegram, см. bot/core/site_auth.py) —
        # нужен в шапке для кнопки "Войти"/имени пользователя, см. base_public.html.
        from bot.core.site_auth import get_user_by_session
        site_user = await get_user_by_session(request.cookies.get("site_session"))
        return templates.TemplateResponse(
            "dashboard.html", {
                "request": request,
                "stats": stats,
                "bot_version": bot_version,
                "parser_enabled": _state.parser_enabled,
                "parse_interval_min": _state.parse_interval_min,
                "parse_interval_max": _state.parse_interval_max,
                "popup_width": app_settings.get_int("POPUP_WIDTH_PX", 380),
                "site_user": site_user,
                "listing_id": listing_id,
                "listing_meta": listing_meta,
            }
        )

    @app.get("/", response_class=HTMLResponse)
    async def root_dashboard(request: Request):
        # Голый домен — самый обычный способ, которым реальный посетитель
        # заходит на сайт. Раньше 302-редиректил на /admin — адресная строка
        # у обычного посетителя тут же показывала "admin", хотя это
        # публичная карта без логина (задача "все не должны работать под
        # админом", 2026-08-12). Рендерим карту прямо тут, без редиректа.
        # /admin остаётся рабочим URL (не ломаем расшаренные ссылки) —
        # просто больше не единственный вход.
        return await _render_dashboard(request)

    @app.get("/admin", response_class=HTMLResponse)
    async def dashboard(request: Request):
        # Публичная страница: карта и фильтры без логина; админ-элементы
        # скрываются в шаблоне через is_admin(request). Оставлен для
        # обратной совместимости расшаренных ссылок — см. "/" выше, теперь
        # тот же контент без "admin" в адресе.
        return await _render_dashboard(request)

    @app.get("/admin/listing/{listing_id}", response_class=HTMLResponse)
    async def listing_page(request: Request, listing_id: str):
        # Единственная страница объявления — та же карта, что и /admin, с
        # автоматически открытым попапом (см. dashboard.html: {% if listing_id %}
        # openDetailModal(...) на DOMContentLoaded, plus history.pushState при
        # открытии/закрытии попапа с карты — делает эту ссылку копируемой и
        # попадаемой сюда напрямую). Раньше было два разных представления
        # объявления (этот попап и отдельная /admin/analytics/{id}) — теперь
        # только это, старый роут ниже редиректит сюда.
        from bot.db.pg import fetchrow as pg_fetchrow

        # Вариант новостройки (см. миграцию 041_newbuild.sql) — отдельная
        # таблица, id с префиксом "nb-" (openDetailModal в dashboard.html
        # различает по нему секондари/новостройку). OG-превью попроще:
        # без title/complex_name объявления, просто комн+площадь+ЖК+цена.
        if listing_id.startswith("nb-"):
            try:
                unit_id_int = int(listing_id[3:])
            except ValueError:
                unit_id_int = None
            unit_row = await pg_fetchrow("""
                SELECT u.rooms, u.area, u.price, u.layout_photo_url, c.name AS complex_name
                FROM newbuild_units u JOIN complexes c ON c.id = u.complex_id
                WHERE u.id = $1
            """, unit_id_int) if unit_id_int is not None else None
            listing_meta = None
            if unit_row:
                r = dict(unit_row)
                price_txt = f"{r['price']/1e6:.1f} млн ₸" if r.get("price") else ""
                title_bits = [f"{r.get('rooms') or '?'}-комн", f"{r.get('area') or '?'} м²", r["complex_name"]]
                listing_meta = {
                    "title": f"{price_txt} · {' · '.join(title_bits)} · Новостройка — Hatuli".strip(" ·"),
                    "description": f"{' · '.join(title_bits)} — цена {price_txt or 'по запросу'} на Hatuli.",
                    "image": r.get("layout_photo_url"),
                }
            return await _render_dashboard(request, listing_id=listing_id, listing_meta=listing_meta)

        row = await pg_fetchrow(
            "SELECT id, title, price, rooms, area, district, complex_name, photos "
            "FROM apartment_listings WHERE id = $1", listing_id)
        listing_meta = None
        if row:
            r = dict(row)
            photos = r.get("photos") or []
            if isinstance(photos, str):
                import json as _j
                try:
                    photos = _j.loads(photos)
                except ValueError:
                    photos = []
            price_txt = f"{r['price']/1e6:.1f} млн ₸" if r.get("price") else ""
            title_bits = [f"{r.get('rooms') or '?'}-комн", f"{r.get('area') or '?'} м²"]
            if r.get("complex_name"):
                title_bits.append(r["complex_name"])
            listing_meta = {
                "title": f"{price_txt} · {' · '.join(title_bits)} — Hatuli".strip(" ·"),
                "description": f"{' · '.join(title_bits)}{', ' + r['district'] if r.get('district') else ''} — цена {price_txt or 'по запросу'} на Hatuli.",
                "image": photos[0] if photos else None,
            }
        return await _render_dashboard(request, listing_id=listing_id, listing_meta=listing_meta)

    @app.get("/admin/users", response_class=HTMLResponse)
    async def users_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        users = await db.get_users_admin()
        return templates.TemplateResponse("users.html", {"request": request, "users": users})

    @app.post("/admin/users/extend")
    async def extend_user(request: Request, user_id: int = Form(...), role: int = Form(...)):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        await db.grant_subscription(user_id, role)
        await db.log_event("grant", f"admin-panel grant user={user_id} role={role}")
        return RedirectResponse(url="/admin/users", status_code=302)

    @app.post("/admin/users/block")
    async def block_user(request: Request, user_id: int = Form(...), blocked: int = Form(...)):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        await db.set_user_blocked(user_id, bool(blocked))
        await db.log_event("block", f"admin-panel block={blocked} user={user_id}")
        return RedirectResponse(url="/admin/users", status_code=302)

    @app.post("/admin/users/delete")
    async def delete_user(request: Request, user_id: int = Form(...)):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        await db.delete_user_cascade(user_id)
        await db.log_event("delete", f"admin-panel delete user={user_id}")
        return RedirectResponse(url="/admin/users", status_code=302)

    @app.get("/admin/subscriptions", response_class=HTMLResponse)
    async def subscriptions_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("subscriptions.html", {"request": request})

    @app.post("/admin/subscriptions")
    async def subscriptions_submit(
        request: Request, user_id: int = Form(...), role: int = Form(...), days: int = Form(...)
    ):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        end = await db.grant_subscription(user_id, role)
        await db.log_event("grant", f"admin-panel form user={user_id} role={role} days={days} end={end}")
        return RedirectResponse(url="/admin/subscriptions", status_code=302)

    @app.get("/admin/logs", response_class=HTMLResponse)
    async def logs_page(request: Request):
        # Page removed from product per user request — redirect to dashboard.
        return RedirectResponse(url="/admin", status_code=302)

    @app.get("/admin/stats/data")
    async def stats_data(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        stats = await db.get_dashboard_stats()
        stats["parser_enabled"] = _state.parser_enabled
        stats["parse_interval_min"] = _state.parse_interval_min
        stats["parse_interval_max"] = _state.parse_interval_max
        return JSONResponse(stats)

    @app.get("/admin/logs/data")
    async def logs_data(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        lines: list[str] = []
        log_path = os.path.abspath(_LOG_FILE)
        if os.path.exists(log_path):
            try:
                from datetime import datetime, timedelta, timezone
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    raw = f.readlines()[-500:]  # read tail, then filter by time
                filtered: list[str] = []
                for ln in raw:
                    # Try to parse timestamp from "2026-04-11 10:30:45,123 LEVEL ..."
                    try:
                        ts_str = ln[:23].replace(",", ".")
                        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
                        if ts >= cutoff:
                            filtered.append(ln)
                    except Exception:
                        filtered.append(ln)  # unparseable line — include it
                lines = filtered[-50:] if filtered else raw[-20:]
            except OSError:
                lines = ["[Не удалось прочитать файл лога]"]
        else:
            lines = [f"[Файл {_LOG_FILE!r} не найден]"]
        return JSONResponse({"lines": [ln.rstrip("\n") for ln in lines]})

    @app.get("/admin/issues", response_class=HTMLResponse)
    async def issues_page(request: Request):
        # Page removed from product per user request — redirect to dashboard.
        return RedirectResponse(url="/admin", status_code=302)

    @app.post("/admin/issues/clear")
    async def issues_clear(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        await db.clear_parse_errors()
        return RedirectResponse(url="/admin/issues", status_code=302)

    @app.get("/admin/users/stats", response_class=HTMLResponse)
    async def users_stats_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        user_stats = await db.get_per_user_stats()
        return templates.TemplateResponse(
            "user_stats.html", {"request": request, "user_stats": user_stats}
        )

    @app.get("/admin/parser/stats", response_class=HTMLResponse)
    async def parser_stats_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        cycle_info = await db.get_parser_cycle_info()
        last_listings = await db.get_last_listings(20)
        return templates.TemplateResponse(
            "parser_stats.html",
            {
                "request": request,
                "cycle_info": cycle_info,
                "last_listings": last_listings,
            },
        )

    # ── Parser controls ────────────────────────────────────────────────────────

    @app.post("/admin/parser/toggle")
    async def parser_toggle(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        _state.parser_enabled = not _state.parser_enabled
        status = "enabled" if _state.parser_enabled else "disabled"
        await db.log_event("parser_control", f"admin toggled parser {status}")
        return RedirectResponse(url="/admin", status_code=302)

    @app.post("/admin/parser/interval")
    async def parser_interval(
        request: Request,
        interval_min: int = Form(...),
        interval_max: int = Form(...),
    ):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        interval_min = max(60, min(interval_min, 1200))
        interval_max = max(interval_min, min(interval_max, 1200))
        _state.parse_interval_min = interval_min
        _state.parse_interval_max = interval_max
        await db.log_event(
            "parser_control",
            f"admin set interval min={interval_min} max={interval_max}",
        )
        return RedirectResponse(url="/admin", status_code=302)


    @app.get("/admin/analytics", response_class=HTMLResponse)
    async def analytics_page(
        request: Request,
        district: str = "",
        rooms: str = "",
        min_score: int = 0,
        sort: str = "score_total",
        limit: int = 10,
        seller: str = "",
    ):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)

        from bot.db.pg import fetch as pg_fetch

        conditions = [
            "score_total IS NOT NULL",
            "is_active IS NOT FALSE",
            "COALESCE(is_duplicate, FALSE) = FALSE",
            "last_seen > now() - interval '14 days'",
        ]
        params = []
        i = 1

        if district:
            conditions.append(f"district ILIKE '%' || ${i} || '%'")
            params.append(district)
            i += 1
        if rooms:
            try:
                conditions.append(f"rooms = ${i}")
                params.append(int(rooms))
                i += 1
            except ValueError:
                pass
        conditions.append(f"score_total >= ${i}")
        params.append(min_score)
        i += 1

        if seller == "owner":
            conditions.append("is_owner IS TRUE")
        elif seller == "agent":
            conditions.append("is_owner IS DISTINCT FROM TRUE")

        valid_sorts = {"score_total", "yield_pct", "price", "bargain_discount_pct"}
        sort_col = sort if sort in valid_sorts else "score_total"

        where = " AND ".join(conditions)
        rows = await pg_fetch(
            f"""
            SELECT id, url, title, rooms, district, complex_name, area, floor, floors_total,
                   price, est_rent, yield_pct, payback_years,
                   score_total, score_yield, score_price_market, score_location,
                   score_apt_type, score_floor, score_complex, score_supply,
                   reasons, is_owner, seller_type,
                   bargain_discount_pct, bargain_rec, bargain_target,
                   rent_source, year_built, is_new_build,
                   first_seen, last_seen
            FROM apartment_listings
            WHERE {where}
            ORDER BY {sort_col} DESC NULLS LAST
            LIMIT {limit if limit in (10, 20, 30) else 10}
            """,
            *params,
        )

        # Гистограмма распределения скоров (по текущим фильтрам, без min_score)
        hist_rows = await pg_fetch(f"""
            SELECT (score_total / 5) * 5 AS bucket, COUNT(*) AS cnt
            FROM apartment_listings
            WHERE {' AND '.join(c for c in conditions if 'score_total >=' not in c)}
              AND score_total IS NOT NULL
            GROUP BY 1 ORDER BY 1
        """, *params[:-1])  # последний параметр — min_score, его не применяем

        # Гистограмма распределения по ЦЕНЕ, отдельно для 1к/2к/3к/4к+
        # (бакеты по млн ₸, ширина зависит от типичного разброса комнатности)
        price_hist_raw = await pg_fetch(f"""
            SELECT
                CASE WHEN rooms >= 4 THEN 4 ELSE COALESCE(rooms, 0) END AS room_bucket,
                (price / 5000000) * 5 AS price_bucket_m,
                COUNT(*) AS cnt
            FROM apartment_listings
            WHERE {' AND '.join(c for c in conditions if 'score_total >=' not in c)}
              AND price > 0 AND rooms IS NOT NULL AND rooms >= 1
            GROUP BY 1, 2
            ORDER BY 1, 2
        """, *params[:-1])
        price_hist = {1: [], 2: [], 3: [], 4: []}
        for r in price_hist_raw:
            rb = int(r["room_bucket"])
            if rb in price_hist:
                price_hist[rb].append({"bucket": r["price_bucket_m"], "cnt": r["cnt"]})

        # Компактный rental index: медиана аренды 1к и 2к
        rental_stats = await pg_fetch("""
            SELECT rooms, median_price, sample_count
            FROM rental_index
            WHERE prop_type = 'apartment' AND rooms IN (1, 2)
            ORDER BY rooms, sample_count DESC
        """)
        rental_summary = {}
        for r in rental_stats:
            k = int(r["rooms"])
            if k not in rental_summary:
                rental_summary[k] = {"price": r["median_price"], "n": 0}
            rental_summary[k]["n"] += r["sample_count"] or 0

        # Общее покрытие данными — раньше жило тайлами на главной карте,
        # перенесено сюда (главная карта — рабочий инструмент поиска, не
        # витрина статистики).
        from bot.db import settings as _app_settings2
        await _app_settings2.load()
        coverage_total = await pg_fetch(
            "SELECT COUNT(*) AS c FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND COALESCE(is_duplicate, FALSE) = FALSE")
        coverage_complexes = await pg_fetch("SELECT COUNT(*) AS c FROM complexes")
        coverage = {
            "total": coverage_total[0]["c"] if coverage_total else 0,
            "complexes": coverage_complexes[0]["c"] if coverage_complexes else 0,
            "krisha_total": _app_settings2.get_int("KRISHA_TOTAL_FOUND", 0),
        }

        return templates.TemplateResponse(
            "analytics.html",
            {
                "request": request,
                "atab": "sales",
                "listings": [dict(r) for r in rows],
                "score_hist": [dict(r) for r in hist_rows],
                "price_hist": price_hist,
                "rental_summary": rental_summary,
                "coverage": coverage,
                "filters": {
                    "district": district,
                    "rooms": rooms,
                    "min_score": min_score,
                    "sort": sort,
                    "limit": limit,
                    "seller": seller,
                },
                "total": len(rows),
            },
        )

    @app.get("/admin/analytics/prices", response_class=HTMLResponse)
    async def prices_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse(
            "prices.html", {"request": request, "atab": "prices"})

    @app.get("/admin/analytics/views", response_class=HTMLResponse)
    async def views_analytics_page(request: Request):
        # ВАЖНО: должен быть объявлен ДО /admin/analytics/{listing_id} ниже —
        # Starlette матчит роуты в порядке регистрации, и этот catch-all
        # (объявлен в этом же модуле раньше include_router с terminal_extras)
        # перехватывал "views"/"floors" как listing_id, отдавая "Not found".
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("views_analytics.html", {
            "request": request, "atab": "views",
        })

    @app.get("/admin/analytics/floors", response_class=HTMLResponse)
    async def floors_analytics_page(request: Request):
        # Объединённая страница: этажи + потолок + этаж-vs-продажи +
        # координаты (раньше 4 отдельные вкладки — /admin/analytics/ceiling,
        # /admin/analytics/floor-performance и /admin/unbound теперь просто
        # редиректят сюда, см. ниже).
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import fetchval as pg_fv
        total_active = await pg_fv(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND COALESCE(is_duplicate, FALSE) = FALSE") or 0
        missing_floor = await pg_fv(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND COALESCE(is_duplicate, FALSE) = FALSE AND floor IS NULL") or 0
        missing_ceiling = await pg_fv(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND COALESCE(is_duplicate, FALSE) = FALSE AND ceiling_height IS NULL") or 0
        unbound_stats = {
            "total_active": total_active,
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
        return templates.TemplateResponse("floors_analytics.html", {
            "request": request, "atab": "floors",
            "total_active": total_active, "missing_floor": missing_floor,
            "missing_ceiling": missing_ceiling, "stats": unbound_stats,
        })

    @app.get("/admin/analytics/ceiling", response_class=HTMLResponse)
    async def ceiling_analytics_page_redirect(request: Request):
        return RedirectResponse(url="/admin/analytics/floors", status_code=301)

    @app.get("/admin/analytics/floor-performance", response_class=HTMLResponse)
    async def floor_performance_page_redirect(request: Request):
        return RedirectResponse(url="/admin/analytics/floors", status_code=301)

    @app.get("/admin/analytics/year", response_class=HTMLResponse)
    async def year_analytics_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import fetchval as pg_fv
        total_active = await pg_fv(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND COALESCE(is_duplicate, FALSE) = FALSE") or 0
        # Год берём с объявления, а если пусто — с его ЖК (см. комментарий
        # в service_apartments.py про снимок year_stats_history).
        missing_year = await pg_fv("""
            SELECT COUNT(*) FROM apartment_listings a
            LEFT JOIN complexes c ON lower(trim(c.name)) = lower(trim(a.complex_name))
            WHERE a.is_active IS NOT FALSE AND COALESCE(a.is_duplicate, FALSE) = FALSE
              AND COALESCE(a.year_built, c.year_built) IS NULL
        """) or 0
        return templates.TemplateResponse("year_analytics.html", {
            "request": request, "atab": "year",
            "total_active": total_active, "missing_year": missing_year,
        })

    @app.get("/admin/analytics/demand", response_class=HTMLResponse)
    async def demand_analytics_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("demand_analytics.html", {
            "request": request, "atab": "demand",
        })


    @app.get("/admin/analytics/hype", response_class=HTMLResponse)
    async def hype_analytics_page_old(request: Request):
        # Слито во вкладку "Хайп" hub-страницы /admin/analytics/heatmaps —
        # см. задачу "reorganize into 4 tabbed hub pages".
        return RedirectResponse(url="/admin/analytics/heatmaps?tab=hype", status_code=301)

    @app.get("/admin/analytics/homeportal", response_class=HTMLResponse)
    async def homeportal_page(request: Request):
        # Слито во вкладку "Homeportal" hub-страницы /admin/parsers —
        # см. задачу "reorganize into 4 tabbed hub pages". Логика/запросы
        # переехали в _homeportal_data() (terminal_extras.py).
        return RedirectResponse(url="/admin/parsers?tab=krisha-homeportal", status_code=301)

    @app.get("/admin/analytics/parse-monitor", response_class=HTMLResponse)
    async def parse_monitor_page(request: Request):
        # Слито во вкладку "ЖК (Крыша)" hub-страницы /admin/parsers —
        # см. задачу "reorganize into 4 tabbed hub pages". Логика/запросы
        # переехали в _parse_monitor_data() (terminal_extras.py).
        return RedirectResponse(url="/admin/parsers?tab=krisha-complex-scan", status_code=301)

    @app.get("/admin/analytics/geo", response_class=HTMLResponse)
    async def geo_analytics_page_old(request: Request):
        # Слито во вкладку "Геоаналитика" hub-страницы /admin/analytics/heatmaps —
        # см. задачу "reorganize into 4 tabbed hub pages".
        return RedirectResponse(url="/admin/analytics/heatmaps?tab=geo", status_code=301)

    @app.get("/admin/analytics/heatmaps", response_class=HTMLResponse)
    async def heatmaps_hub_page(request: Request, tab: str = "hype"):
        # ВАЖНО: ДОЛЖЕН стоять выше catch-all /admin/analytics/{listing_id} —
        # см. комментарий у transport_analytics_page ниже.
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        if tab not in ("hype", "geo"):
            tab = "hype"
        return templates.TemplateResponse("heatmaps_hub.html", {
            "request": request, "atab": tab, "tab": tab,
        })

    @app.get("/admin/analytics/photo-analysis", response_class=HTMLResponse)
    async def photo_analysis_page(request: Request):
        # ВАЖНО: выше catch-all /admin/analytics/{listing_id} ниже.
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import fetchval as pg_fetchval, fetch as pg_fetch
        batch = await pg_fetchval("SELECT value FROM app_settings WHERE key='FLOORPLAN_BATCH'") or "200"
        delay = await pg_fetchval("SELECT value FROM app_settings WHERE key='FLOORPLAN_DELAY'") or "1.0"
        queue = await pg_fetchval("""
            SELECT count(*) FROM apartment_listings
            WHERE floorplan_checked_at IS NULL AND photos IS NOT NULL AND photos::text != '[]'
              AND is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE""") or 0
        hourly = await pg_fetch("""
            SELECT date_trunc('hour', floorplan_checked_at) AS h, count(*) AS cnt
            FROM apartment_listings WHERE floorplan_checked_at > now() - interval '24 hours'
            GROUP BY 1 ORDER BY 1""")
        return templates.TemplateResponse("photo_analysis.html", {
            "request": request, "atab": "photo_analysis",
            "floorplan_batch": int(float(batch)), "floorplan_delay": float(delay),
            "floorplan_queue": queue,
            "floorplan_hourly": {"labels": [h["h"].strftime("%d.%m %H:00") for h in hourly],
                                  "values": [h["cnt"] for h in hourly]},
        })

    @app.post("/admin/analytics/photo-analysis/settings")
    async def photo_analysis_settings_save(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        data = await request.json()
        from bot.db.pg import execute as pg_execute
        try:
            batch = max(10, min(500, int(float(data.get("batch", 200)))))
            delay = max(0.2, min(5.0, float(data.get("delay", 1.0))))
        except (TypeError, ValueError):
            return JSONResponse({"error": "bad value"}, status_code=400)
        await pg_execute(
            "INSERT INTO app_settings (key, value) VALUES ('FLOORPLAN_BATCH', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = $1", str(batch))
        await pg_execute(
            "INSERT INTO app_settings (key, value) VALUES ('FLOORPLAN_DELAY', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = $1", str(delay))
        return JSONResponse({"ok": True, "batch": batch, "delay": delay})

    @app.get("/admin/analytics/news-analysis", response_class=HTMLResponse)
    async def news_analysis_page(request: Request):
        # Страница слита с вкладкой "Хайп" hub-страницы /admin/analytics/heatmaps
        # (график "Новостей проанализировано" переехал туда, "Токенов
        # потрачено" убран по запросу) — оставляем редирект, чтобы старые
        # ссылки не 404-или.
        return RedirectResponse(url="/admin/analytics/heatmaps?tab=hype", status_code=301)

    @app.get("/admin/analytics/transport", response_class=HTMLResponse)
    async def transport_page(request: Request):
        # ВАЖНО: выше catch-all /admin/analytics/{listing_id} ниже.
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("transport_analytics.html", {
            "request": request, "atab": "transport",
        })

    @app.get("/admin/analytics/transport", response_class=HTMLResponse)
    async def transport_analytics_page(request: Request):
        # ВАЖНО: ДОЛЖЕН стоять выше catch-all /admin/analytics/{listing_id} —
        # без этого "transport" матчился туда как несуществующий listing_id
        # (терялся, т.к. одноимённый роут в terminal_extras.py регистрируется
        # через include_router ПОСЛЕ этого catch-all).
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("transport_analytics.html", {
            "request": request, "atab": "transport",
        })

    async def _keyword_rows_with_counts(pg_fetch, category: str, text_source: str = "listings") -> list[dict]:
        """Слова категории из ai_keywords + живой счётчик упоминаний —
        для apartment_feature/finish считаем по apartment_listings
        (title/description), для complex_feature — тоже по apartment_listings,
        но только там, где complex_name заполнен (это и есть тот самый текст,
        из которого apply_complex_facts() вытаскивает факты про ЖК —
        в самих complexes нет свободно-текстового поля описания)."""
        rows = await pg_fetch(
            "SELECT word FROM ai_keywords WHERE category = $1 ORDER BY word", category)
        out = []
        for r in rows:
            word = r["word"]
            like = f"%{word}%"
            if text_source == "complex":
                cnt = await pg_fetch(
                    "SELECT COUNT(*) AS c FROM apartment_listings WHERE is_active IS NOT FALSE "
                    "AND complex_name IS NOT NULL "
                    "AND (title ILIKE $1 OR description ILIKE $1)", like)
            else:
                cnt = await pg_fetch(
                    "SELECT COUNT(*) AS c FROM apartment_listings WHERE is_active IS NOT FALSE "
                    "AND (title ILIKE $1 OR description ILIKE $1)", like)
            out.append({"word": word, "count": cnt[0]["c"] if cnt else 0})
        return out

    @app.get("/admin/analytics/ai-analysis", response_class=HTMLResponse)
    async def ai_analysis_status_page_old(request: Request):
        # Слито во вкладку "AI-анализ описаний" hub-страницы /admin/analytics/ai —
        # см. задачу "reorganize into 4 tabbed hub pages".
        return RedirectResponse(url="/admin/analytics/ai?tab=analysis", status_code=301)

    async def _ai_analysis_data():
        from bot.db.pg import fetch as pg_fetch, fetchrow as pg_fetchrow
        from bot.db import settings as app_settings
        await app_settings.load()

        counts = await pg_fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE is_active IS NOT FALSE AND description IS NOT NULL AND length(description) > 80) AS eligible,
                COUNT(*) FILTER (WHERE ai_analysis IS NOT NULL) AS processed,
                COUNT(*) FILTER (WHERE (ai_analysis->>'is_relayout')::boolean) AS relayout_cnt,
                COUNT(*) FILTER (WHERE (ai_analysis->>'is_relayout_legal')::boolean) AS relayout_legal_cnt,
                COUNT(*) FILTER (WHERE (ai_analysis->>'is_free_layout')::boolean) AS free_layout_cnt,
                COUNT(*) FILTER (WHERE (ai_analysis->>'has_ac')::boolean) AS ac_cnt,
                COUNT(*) FILTER (WHERE ai_analysis->>'layout' = 'распашонка') AS cross_layout_cnt,
                COUNT(*) FILTER (WHERE layer_details->'layout' IS NOT NULL) AS layout_bonus_cnt
            FROM apartment_listings
        """)
        finish_rows = await pg_fetch("""
            SELECT COALESCE(ai_analysis->>'finish', 'не определено') AS v, COUNT(*) AS cnt
            FROM apartment_listings WHERE ai_analysis IS NOT NULL GROUP BY 1 ORDER BY 2 DESC
        """)
        # Динамика разбора по дням (последние 30 дней) — сколько описаний
        # обработал AI-слой каждый день, см. ai_analyzed_at в ai_text_analysis.py.
        daily_ai_rows = await pg_fetch("""
            SELECT date_trunc('day', ai_analyzed_at)::date AS day, COUNT(*) AS cnt
            FROM apartment_listings
            WHERE ai_analyzed_at IS NOT NULL AND ai_analyzed_at > now() - interval '30 days'
            GROUP BY 1 ORDER BY 1
        """)
        urgency_rows = await pg_fetch("""
            SELECT COALESCE(ai_analysis->>'urgency', 'не определено') AS v, COUNT(*) AS cnt
            FROM apartment_listings WHERE ai_analysis IS NOT NULL GROUP BY 1 ORDER BY 2 DESC
        """)
        complexes_enriched = await pg_fetchrow(
            "SELECT COUNT(*) AS cnt FROM complexes WHERE ai_features IS NOT NULL")
        recent_facts = await pg_fetch("""
            SELECT c.name AS complex_name, kv.key AS field,
                   kv.value->>'value' AS value, kv.value->>'source_url' AS source_url,
                   (kv.value->>'added_at')::timestamptz AS added_at
            FROM complexes c, jsonb_each(c.ai_features) AS kv
            WHERE c.ai_features IS NOT NULL
            ORDER BY added_at DESC LIMIT 30
        """)
        field_labels = {
            "location": "локация", "nearby": "что рядом", "architecture": "архитектура/дизайн",
            "lobby": "холл", "security": "охрана", "concierge": "консьерж",
            "closed_yard": "закрытый двор", "playground": "детская площадка",
            "parking": "паркинг", "closed_territory": "закрытая территория",
        }
        apartment_feature_words = await _keyword_rows_with_counts(pg_fetch, "apartment_feature", "listings")
        finish_words = await _keyword_rows_with_counts(pg_fetch, "finish", "listings")
        complex_feature_words = await _keyword_rows_with_counts(pg_fetch, "complex_feature", "complex")
        return {
            "ai_enabled": app_settings.get_bool("AI_TEXT_ANALYSIS", False),
            "counts": dict(counts) if counts else {},
            "finish_rows": [dict(r) for r in finish_rows],
            "urgency_rows": [dict(r) for r in urgency_rows],
            "complexes_enriched": complexes_enriched["cnt"] if complexes_enriched else 0,
            "recent_facts": [dict(r) for r in recent_facts],
            "field_labels": field_labels,
            "daily_ai": [{"day": r["day"].strftime("%d.%m"), "cnt": r["cnt"]} for r in daily_ai_rows],
            "apartment_feature_words": apartment_feature_words,
            "finish_words": finish_words,
            "complex_feature_words": complex_feature_words,
        }

    @app.post("/admin/analytics/ai-analysis/keywords/add")
    async def ai_analysis_keyword_add(request: Request, category: str = Form(...), word: str = Form(...)):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import execute as pg_exec
        word = word.strip().lower()
        if word and category in ("apartment_feature", "finish", "complex_feature"):
            await pg_exec(
                "INSERT INTO ai_keywords (category, word) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                category, word)
        return RedirectResponse(url="/admin/analytics/ai?tab=analysis", status_code=303)

    @app.post("/admin/analytics/ai-analysis/keywords/delete")
    async def ai_analysis_keyword_delete(request: Request, category: str = Form(...), word: str = Form(...)):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import execute as pg_exec
        await pg_exec("DELETE FROM ai_keywords WHERE category = $1 AND word = $2", category, word)
        return RedirectResponse(url="/admin/analytics/ai?tab=analysis", status_code=303)

    @app.get("/admin/analytics/ai-status", response_class=HTMLResponse)
    async def ai_status_page_old(request: Request):
        # Слито во вкладку "Статус слоёв" hub-страницы /admin/analytics/ai —
        # см. задачу "reorganize into 4 tabbed hub pages".
        return RedirectResponse(url="/admin/analytics/ai?tab=status", status_code=301)

    async def _ai_status_data():
        from bot.db.pg import fetch as pg_fetch, fetchrow as pg_fetchrow
        from bot.db import settings as app_settings
        await app_settings.load()

        desc_counts = await pg_fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE is_active IS NOT FALSE AND description IS NOT NULL AND length(description) > 80) AS eligible,
                COUNT(*) FILTER (WHERE ai_analysis IS NOT NULL) AS processed,
                MAX(ai_analyzed_at) AS last_run
            FROM apartment_listings
        """)
        complexes_enriched = await pg_fetchrow(
            "SELECT COUNT(*) AS cnt FROM complexes WHERE ai_features IS NOT NULL")
        last_fact = await pg_fetchrow("""
            SELECT MAX((kv.value->>'added_at')::timestamptz) AS last_at
            FROM complexes c, jsonb_each(c.ai_features) AS kv
            WHERE c.ai_features IS NOT NULL
        """)
        finish_type_cnt = await pg_fetchrow(
            "SELECT COUNT(*) AS cnt FROM apartment_listings WHERE finish_type IS NOT NULL")
        fp_counts = await pg_fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE floorplan_checked_at IS NOT NULL) AS processed,
                COUNT(*) FILTER (WHERE floorplan_url IS NOT NULL) AS found,
                MAX(floorplan_checked_at) AS last_run
            FROM apartment_listings
        """)
        return {
            "desc_counts": dict(desc_counts) if desc_counts else {},
            "complexes_enriched": complexes_enriched["cnt"] if complexes_enriched else 0,
            "last_fact_at": last_fact["last_at"] if last_fact else None,
            "finish_type_cnt": finish_type_cnt["cnt"] if finish_type_cnt else 0,
            "fp_counts": dict(fp_counts) if fp_counts else {},
            "ai_text_analysis_on": app_settings.get_bool("AI_TEXT_ANALYSIS", False),
            "ai_complex_facts_on": app_settings.get_bool("AI_COMPLEX_FACTS", True),
            "ai_finish_classify_on": app_settings.get_bool("AI_FINISH_CLASSIFY", True),
            "ai_floorplan_scan_on": app_settings.get_bool("AI_FLOORPLAN_SCAN", True),
        }

    @app.get("/admin/analytics/ai", response_class=HTMLResponse)
    async def ai_hub_page(request: Request, tab: str = "analysis"):
        # ВАЖНО: ДОЛЖЕН стоять выше catch-all /admin/analytics/{listing_id} —
        # см. комментарий у transport_analytics_page выше.
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        if tab not in ("analysis", "status"):
            tab = "analysis"
        ctx = {"request": request, "atab": "ai_analysis" if tab == "analysis" else "ai_status", "tab": tab}
        if tab == "status":
            ctx.update(await _ai_status_data())
        else:
            ctx.update(await _ai_analysis_data())
        return templates.TemplateResponse("ai_hub.html", ctx)

    async def _housing_class_rows():
        # Раньше рендерилась напрямую на /admin/analytics/housing-class,
        # теперь — данные для вкладки "Класс жилья" hub-страницы
        # /admin/analytics/complexes (см. complexes_hub_page ниже).
        from bot.db.pg import fetch as pg_fetch
        from bot.core.housing_class_score import compute_housing_class_scores

        rows = await pg_fetch("""
            SELECT c.id, c.name, c.district, c.avg_price_m2::float AS price_per_m2,
                   COALESCE(cts.floors_total, agg.floors_total) AS floors_total,
                   COALESCE(cts.ceiling_height_max, agg.ceiling_height)::float AS ceiling_height,
                   hct.elevator_count, hct.elevator_capacity_kg, hct.elevator_passenger, hct.elevator_freight, hct.apartment_count,
                   hct.entrances,
                   hct.rooms_1, hct.rooms_2, hct.rooms_3, hct.rooms_4,
                   cts.lifts_type, cts.construction_type,
                   COALESCE(agg.listings_count, 0) AS listings_count,
                   (c.krisha_url IS NOT NULL) AS has_krisha,
                   EXISTS (SELECT 1 FROM homeportal_objects h WHERE h.matched_complex_id = c.id) AS has_hp
            FROM complexes c
            LEFT JOIN complex_tech_specs cts ON cts.complex_id = c.id
            LEFT JOIN housing_class_test hct ON hct.complex_id = c.id
            LEFT JOIN (
                SELECT lower(trim(complex_name)) AS key,
                       MAX(floors_total) FILTER (WHERE is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE) AS floors_total,
                       AVG(ceiling_height) FILTER (WHERE is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE) AS ceiling_height,
                       COUNT(*) FILTER (WHERE is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE) AS listings_count
                FROM apartment_listings
                WHERE complex_name IS NOT NULL
                GROUP BY lower(trim(complex_name))
            ) agg ON agg.key = lower(trim(c.name))
            WHERE c.avg_price_m2 IS NOT NULL OR agg.floors_total IS NOT NULL
            ORDER BY c.name
        """)
        complexes = compute_housing_class_scores([dict(r) for r in rows])
        # Сортировка по количеству объявлений (больше всего — сверху), не по скору —
        # см. задачу "housing-class table sort + count column".
        complexes.sort(key=lambda r: -(r.get("listings_count") or 0))
        return complexes

    @app.get("/admin/analytics/housing-class", response_class=HTMLResponse)
    async def housing_class_page_old(request: Request):
        # Слито во вкладку "Класс жилья" hub-страницы /admin/analytics/complexes —
        # см. задачу "reorganize into 4 tabbed hub pages".
        return RedirectResponse(url="/admin/analytics/complexes?tab=housing_class", status_code=301)

    @app.post("/admin/analytics/housing-class/{complex_id}/update")
    async def housing_class_update(request: Request, complex_id: int,
                                    elevator_count: str = Form(""), elevator_capacity_kg: str = Form(""),
                                    elevator_passenger: str = Form(""), elevator_freight: str = Form(""),
                                    apartment_count: str = Form(""), entrances: str = Form(""),
                                    rooms_1: str = Form(""), rooms_2: str = Form(""),
                                    rooms_3: str = Form(""), rooms_4: str = Form("")):
        # Тестовые поля, которых нет больше нигде в БД (лифты/их грузоподъёмность/
        # кол-во квартир) — вводятся вручную здесь же на странице, см. миграции
        # housing_class_test / 026_elevator_capacity.
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import execute as pg_execute

        def _to_int(s: str):
            s = (s or "").strip()
            return int(s) if s.isdigit() else None

        await pg_execute("""
            INSERT INTO housing_class_test (complex_id, elevator_count, elevator_capacity_kg, elevator_passenger, elevator_freight, apartment_count, entrances, rooms_1, rooms_2, rooms_3, rooms_4, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now())
            ON CONFLICT (complex_id) DO UPDATE
            SET elevator_count = $2, elevator_capacity_kg = $3, elevator_passenger = $4, elevator_freight = $5,
                apartment_count = $6, entrances = $7, rooms_1 = $8, rooms_2 = $9, rooms_3 = $10, rooms_4 = $11, updated_at = now()
        """, complex_id, _to_int(elevator_count), _to_int(elevator_capacity_kg), _to_int(elevator_passenger), _to_int(elevator_freight),
            _to_int(apartment_count), _to_int(entrances), _to_int(rooms_1), _to_int(rooms_2), _to_int(rooms_3), _to_int(rooms_4))
        return RedirectResponse(url="/admin/analytics/complexes?tab=housing_class", status_code=303)

    @app.post("/admin/analytics/housing-class/{complex_id}/delete")
    async def housing_class_delete(request: Request, complex_id: int):
        # Удаление явно несуществующих/мусорных ЖК из базы — только карточка
        # ЖК (housing_class_test/complex_tech_specs каскадно), сами объявления
        # НЕ трогаем: complex_name у apartment_listings — свободный текст, не FK.
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import execute as pg_execute
        await pg_execute("DELETE FROM complexes WHERE id = $1", complex_id)
        return RedirectResponse(url="/admin/analytics/complexes?tab=housing_class", status_code=303)

    @app.post("/admin/analytics/housing-class/bulk-delete")
    async def housing_class_bulk_delete(request: Request, ids: list[int] = Form(...)):
        # Массовое удаление отмеченных галочками ЖК (см. housing_class_delete
        # выше — та же семантика, только скопом; вызывается через fetch с
        # location.reload(), поэтому просто отвечаем 204, без редиректа.
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import execute as pg_execute
        await pg_execute("DELETE FROM complexes WHERE id = ANY($1::int[])", ids)
        return JSONResponse({"deleted": len(ids)})

    # ── ЖК под правку: объявления, всё ещё привязанные к мусорным именам ──

    async def _complexes_fix_data():
        # После консолидации complexes (дедуп/переименование по Крыше) часть
        # объявлений осталась привязана к complex_name, который совпадает с
        # уже помеченной is_garbage строкой из ПРЕДЫДУЩИХ чисток — обычно
        # обрывок описания/эмодзи-реклама, а не настоящее имя ЖК. Разобрать
        # такое автоматом рискованно (нет надёжного сигнала, какой это
        # реальный ЖК), поэтому — ручная поштучная правка здесь.
        # Раньше рендерилась напрямую на /admin/complexes-fix, теперь —
        # данные для вкладки "ЖК под правку" hub-страницы
        # /admin/analytics/complexes (см. complexes_hub_page ниже).
        from bot.db.pg import fetch as pg_fetch

        rows = await pg_fetch("""
            SELECT a.id, a.title, a.address, a.district, a.complex_name, a.url,
                   a.price, a.area, a.rooms
            FROM apartment_listings a
            JOIN complexes c ON lower(btrim(c.name)) = lower(btrim(a.complex_name))
            WHERE c.is_garbage = true AND a.is_active IS NOT FALSE
            ORDER BY a.complex_name, a.id
        """)

        def _suggest(name: str) -> str:
            # Черновая подсказка для поля ввода: обрезаем по первому эмодзи/
            # спецсимволу-разделителю рекламного хвоста — админ всё равно
            # проверяет и правит руками, это просто чтобы не печатать с нуля.
            import re as _re
            m = _re.match(r"^[\w\-\.]+(?:[ \-][\w\-\.]+){0,3}", name.strip())
            suggested = (m.group(0).strip() if m else name.strip())[:60]
            # БАГ (найден): для коротких мусорных имён без разделителя-хвоста
            # (напр. "Sunset Avenue" — само уже похоже на имя ЖК) regex не
            # обрезает ничего, suggested == name. Если админ жмёт "Применить
            # ко всей группе" не глядя (доверяя подсказке), reassign-group
            # переписывает complex_name на ТО ЖЕ САМОЕ значение — запись
            # остаётся привязана к тому же is_garbage=true complexes.name и
            # после обновления страницы снова тут же, будто кнопка не
            # работает. Пустое поле вместо бесполезной "подсказки" заставляет
            # ввести реальное имя, а не молча ничего не поменять.
            if suggested.strip().lower() == name.strip().lower():
                return ""
            return suggested

        groups: dict[str, list] = {}
        for r in rows:
            d = dict(r)
            groups.setdefault(d["complex_name"], []).append(d)

        all_names = await pg_fetch(
            "SELECT name FROM complexes WHERE is_garbage = false ORDER BY name")

        return {
            "groups": [{"garbage_name": k, "suggested": _suggest(k), "listings": v}
                       for k, v in sorted(groups.items())],
            "total": len(rows),
            "all_names": [r["name"] for r in all_names],
        }

    @app.get("/admin/complexes-fix", response_class=HTMLResponse)
    async def complexes_fix_page_old(request: Request):
        # Слито во вкладку "ЖК под правку" hub-страницы /admin/analytics/complexes —
        # см. задачу "reorganize into 4 tabbed hub pages".
        return RedirectResponse(url="/admin/analytics/complexes?tab=complexes_fix", status_code=301)

    @app.get("/admin/analytics/complexes", response_class=HTMLResponse)
    async def complexes_hub_page(request: Request, tab: str = "housing_class"):
        # ВАЖНО: ДОЛЖЕН стоять выше catch-all /admin/analytics/{listing_id} —
        # см. комментарий у transport_analytics_page выше.
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        if tab not in ("housing_class", "complexes_fix"):
            tab = "housing_class"
        ctx = {"request": request, "atab": tab, "tab": tab}
        if tab == "complexes_fix":
            ctx.update(await _complexes_fix_data())
        else:
            ctx["complexes"] = await _housing_class_rows()
            # Сводка сверху страницы: сколько ЖК реально закрыты данными по
            # квартирам/этажности/подъездам — нужна была, чтобы видеть охват
            # без прокрутки всей таблицы (тем более entrances — новое поле,
            # почти везде ещё не заполнено вручную).
            from bot.db.pg import fetchrow as pg_fetchrow
            coverage_row = await pg_fetchrow("""
                SELECT
                  count(*) FILTER (WHERE hc.apartment_count IS NOT NULL OR ho.apartments_total IS NOT NULL) AS with_apt_count,
                  count(*) FILTER (WHERE cts.floors_total IS NOT NULL) AS with_floors,
                  count(*) FILTER (WHERE hc.entrances IS NOT NULL) AS with_entrances,
                  count(*) AS total
                FROM complexes c
                LEFT JOIN housing_class_test hc ON hc.complex_id = c.id
                LEFT JOIN complex_tech_specs cts ON cts.complex_id = c.id
                LEFT JOIN (
                    SELECT matched_complex_id, SUM(apartments_total) AS apartments_total
                    FROM homeportal_objects WHERE matched_complex_id IS NOT NULL
                    GROUP BY matched_complex_id
                ) ho ON ho.matched_complex_id = c.id
            """)
            ctx["coverage"] = dict(coverage_row) if coverage_row else None
        return templates.TemplateResponse("complexes_hub.html", ctx)

    @app.post("/admin/complexes-fix/reassign")
    async def complexes_fix_reassign(request: Request, listing_id: str = Form(...),
                                      new_complex_name: str = Form(...)):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        new_complex_name = new_complex_name.strip()
        if not new_complex_name:
            return JSONResponse({"error": "empty name"}, status_code=400)
        from bot.db.pg import execute as pg_execute
        await pg_execute(
            "UPDATE apartment_listings SET complex_name = $1 WHERE id = $2",
            new_complex_name, listing_id)
        return JSONResponse({"ok": True})

    @app.post("/admin/complexes-fix/reassign-group")
    async def complexes_fix_reassign_group(request: Request, garbage_name: str = Form(...),
                                            new_complex_name: str = Form(...)):
        # То же самое, но для всей группы объявлений с одним и тем же
        # мусорным именем разом — когда админ уверен, что это один ЖК.
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        new_complex_name = new_complex_name.strip()
        if not new_complex_name:
            return JSONResponse({"error": "empty name"}, status_code=400)
        from bot.db.pg import execute as pg_execute
        result = await pg_execute(
            "UPDATE apartment_listings SET complex_name = $1 WHERE lower(btrim(complex_name)) = lower(btrim($2))",
            new_complex_name, garbage_name)
        return JSONResponse({"ok": True, "result": result})

    # Страница сноса перенесена в /admin/info#demolition — тут не дублируем
    # (маршрут закомментирован, API /admin/api/demolition-points остаётся для карты)
    @app.get("/admin/analytics/demolition", response_class=HTMLResponse)
    async def demolition_page(request: Request):
        """Снос/реновация — перенесено в /admin/info#demolition."""
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return RedirectResponse(url="/admin/info#demolition", status_code=302)
        year = request.query_params.get("year", "")
        district = request.query_params.get("district", "")
        conds, params = ["1=1"], []
        if year.isdigit():
            conds.append(f"demolish_year = ${len(params)+1}"); params.append(int(year))
        if district:
            conds.append(f"district = ${len(params)+1}"); params.append(district)
        rows = await pg_fetch(f"""
            SELECT id, address, district, apartments, demolish_year, year_built, wear_pct, lat, lon
            FROM demolition_houses WHERE {' AND '.join(conds)}
            ORDER BY demolish_year, address
        """, *params)
        stats = await pg_fetch("""
            SELECT demolish_year AS year, COUNT(*) AS houses, COALESCE(SUM(apartments),0) AS flats
            FROM demolition_houses GROUP BY 1 ORDER BY 1
        """)
        districts = await pg_fetch("SELECT DISTINCT district FROM demolition_houses ORDER BY 1")
        totals = await pg_fetch("""
            SELECT COUNT(*) AS houses, COALESCE(SUM(apartments),0) AS flats FROM demolition_houses
        """)
        houses = []
        for r in rows:
            d = dict(r)
            if d.get("wear_pct") is not None:
                d["wear_pct"] = float(d["wear_pct"])
            houses.append(d)
        return templates.TemplateResponse("demolition.html", {
            "request": request,
            "atab": "demolition",
            "houses": houses,
            "stats": [dict(r) for r in stats],
            "districts": [d["district"] for d in districts],
            "total_houses": totals[0]["houses"] if totals else 0,
            "total_flats": totals[0]["flats"] if totals else 0,
            "f_year": year, "f_district": district,
        })

    @app.get("/admin/analytics/genplan", response_class=HTMLResponse)
    async def genplan_page(request: Request):
        """Генплан Астаны: корректировка до 2030, источники, сигналы."""
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("genplan.html", {
            "request": request,
            "atab": "genplan",
        })

    # ── "Основное": сводка работоспособности всего проекта одним взглядом ──
    # (парсеры/каналы данных/AI-сервисы/бекап) — первая вкладка слева в
    # админ-панели, см. _analytics_tabs.html. Раньше эти сигналы были
    # разбросаны по десятку разных страниц (/admin/parsers, /admin/backup,
    # /admin/analytics/ai, systemctl руками) — не было ни одной страницы,
    # где сразу видно "всё ли ок". ВАЖНО: должен стоять выше catch-all
    # /admin/analytics/{listing_id} ниже — иначе "overview" матчится как id.
    async def _krisha_units_status() -> dict:
        # Динамическое обнаружение ВСЕХ юнитов krisha-* (а не хардкод
        # списка) — новый парсер/сервис появится в этой таблице сам,
        # без правки кода. Отдельно вытягиваем таймеры: сервисы, которых
        # они триггерят, ЗАКОНОМЕРНО простаивают (inactive/dead) между
        # запусками — для них "ок" значит "последний запуск успешен"
        # (systemctl Result=success), а не "сейчас активен".
        import asyncio as _aio
        proc = await _aio.create_subprocess_exec(
            "systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain", "krisha-*",
            stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        raw_services = []
        for line in out.decode(errors="replace").splitlines():
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue
            unit, _load, active, sub = parts[:4]
            desc = parts[4] if len(parts) > 4 else ""
            raw_services.append({"unit": unit, "active": active, "sub": sub, "desc": desc})

        proc2 = await _aio.create_subprocess_exec(
            "systemctl", "list-timers", "--all", "--no-legend", "--plain", "krisha-*",
            stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.DEVNULL,
        )
        out2, _ = await proc2.communicate()
        timer_driven = {}
        for line in out2.decode(errors="replace").splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            svc, timer_unit = parts[-1], parts[-2]
            timer_driven[svc] = timer_unit

        services = []
        for s in raw_services:
            unit = s["unit"]
            if unit in timer_driven:
                proc3 = await _aio.create_subprocess_exec(
                    "systemctl", "show", unit, "-p", "Result", "--value",
                    stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.DEVNULL,
                )
                out3, _ = await proc3.communicate()
                result = out3.decode().strip()
                services.append({
                    "unit": unit, "desc": s["desc"], "periodic": True,
                    "timer": timer_driven[unit], "ok": result == "success",
                    "detail": f"таймер {timer_driven[unit]}, последний запуск: {result or '—'}",
                })
            else:
                ok = s["active"] in ("active", "activating")
                services.append({
                    "unit": unit, "desc": s["desc"], "periodic": False,
                    "ok": ok, "detail": s["active"] + "/" + s["sub"],
                })
        return {"services": services, "all_ok": all(s["ok"] for s in services)}

    @app.get("/admin/analytics/overview", response_class=HTMLResponse)
    async def overview_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import fetch as pg_fetch, fetchval as pg_fv, fetchrow as pg_fr
        from bot.db import settings as app_settings

        units = await _krisha_units_status()

        # ── Каналы данных: последний успешный приём + вердикт "пускает ли
        # источник парсить" — если сервис жив, но давно нет новых данных,
        # это не "нет новых объявлений", а скорее всего блокировка/капча.
        now_row = await pg_fr("SELECT now() AS n")
        now_ts = now_row["n"]

        async def _channel(name: str, last_ts, stale_after_hours: float, extra: str = ""):
            if last_ts is None:
                return {"name": name, "last": None, "ok": None, "extra": extra,
                        "verdict": "нет данных вовсе"}
            age_h = (now_ts - last_ts).total_seconds() / 3600.0
            ok = age_h <= stale_after_hours
            return {
                "name": name, "last": last_ts.strftime("%d.%m %H:%M"), "ok": ok, "extra": extra,
                "verdict": "пускает" if ok else f"нет новых данных {age_h:.0f}ч — возможно блокирует",
            }

        sale_last = await pg_fv("SELECT MAX(first_seen) FROM apartment_listings")
        rental_last = await pg_fv("SELECT MAX(found_at) FROM rental_listings")
        korter_last = await pg_fv("SELECT MAX(updated_at) FROM complexes WHERE source_info->'korter' IS NOT NULL")
        homsters_last = await pg_fv("SELECT MAX(updated_at) FROM complexes WHERE source_info->'homsters' IS NOT NULL")
        homeportal_last = None
        try:
            homeportal_last = await pg_fv("SELECT MAX(ts) FROM homeportal_parse_log")
        except Exception:
            pass
        market_last = await pg_fv("SELECT MAX(updated_at) FROM banks")
        hype_news_last = None
        try:
            hype_news_last = await pg_fv("SELECT MAX(ts) FROM news")
        except Exception:
            pass

        channels = [
            await _channel("Крыша — продажа", sale_last, 6),
            await _channel("Крыша — аренда", rental_last, 6),
            await _channel("Korter.kz", korter_last, 48),
            await _channel("Homsters.kz", homsters_last, 48),
            await _channel("Homeportal.kz", homeportal_last, 72),
            await _channel("Рыночные данные (НБРК/КДИФ/Отбасы/stat.gov.kz)", market_last, 24 * 10),
            await _channel("Новости (хайп-трекер)", hype_news_last, 30),
        ]

        # ── 24ч графики: сколько объявлений спаршено, по часам ─────────────
        sale_hourly = await pg_fetch("""
            SELECT date_trunc('hour', first_seen) AS h, COUNT(*) AS cnt
            FROM apartment_listings WHERE first_seen > now() - interval '24 hours'
            GROUP BY 1 ORDER BY 1""")
        rental_hourly = await pg_fetch("""
            SELECT date_trunc('hour', found_at) AS h, COUNT(*) AS cnt
            FROM rental_listings WHERE found_at > now() - interval '24 hours'
            GROUP BY 1 ORDER BY 1""")

        # ── AI-сервисы: включён ли тумблер + свежесть последней обработки ──
        await app_settings.load()
        ai_text_last = await pg_fv("SELECT MAX(ai_analyzed_at) FROM apartment_listings")
        floorplan_last = await pg_fv("SELECT MAX(floorplan_checked_at) FROM apartment_listings")

        def _ai_row(label: str, setting_key: str, last_ts, stale_after_hours: float = 48):
            enabled = app_settings.get_bool(setting_key)
            if not enabled:
                return {"label": label, "enabled": False, "ok": None, "detail": "выключено в настройках"}
            if last_ts is None:
                return {"label": label, "enabled": True, "ok": False, "detail": "включено, но данных нет"}
            age_h = (now_ts - last_ts).total_seconds() / 3600.0
            ok = age_h <= stale_after_hours
            return {"label": label, "enabled": True, "ok": ok,
                    "detail": f"последняя обработка: {last_ts.strftime('%d.%m %H:%M')}" + ("" if ok else f" ({age_h:.0f}ч назад)")}

        ai_rows = [
            _ai_row("Анализ текста объявлений (LLM)", "AI_TEXT_ANALYSIS", ai_text_last),
            _ai_row("Детекция планировок на фото", "AI_FLOORPLAN_SCAN", floorplan_last, 12),
            _ai_row("Разбор новостей (хайп)", "AI_HYPE_NEWS", hype_news_last, 30),
        ]
        for key, label in (("AI_COMPLEX_FACTS", "Факты о ЖК"), ("AI_FINISH_CLASSIFY", "Классификация отделки")):
            enabled = app_settings.get_bool(key)
            ai_rows.append({"label": label, "enabled": enabled, "ok": None,
                             "detail": "включено" if enabled else "выключено в настройках"})
        deepseek_key_set = bool(os.getenv("DEEPSEEK_API_KEY"))

        # "N объявлений на карте" — раньше показывалось прямо в строке
        # фильтров на публичной карте, убрали оттуда (см. задачу), тот же
        # запрос (см. /admin/api/map-points, with_coords) — сюда.
        map_listings_count = await pg_fv(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND COALESCE(is_duplicate, FALSE) = FALSE AND lat IS NOT NULL") or 0

        # ── Бекап — последние 5 (было LIMIT 1/одна строка) ─────────────────
        backup_rows = await pg_fetch("""
            SELECT ts, status, kind FROM backup_history ORDER BY ts DESC LIMIT 5
        """)
        backup_list = []
        for row in backup_rows:
            age_h = (now_ts - row["ts"]).total_seconds() / 3600.0
            backup_list.append({
                "ts": row["ts"].strftime("%d.%m %H:%M"), "status": row["status"],
                "kind": row["kind"], "age_h": round(age_h, 1),
                "ok": row["status"] == "ok" and age_h <= 48,
            })

        return templates.TemplateResponse("overview.html", {
            "request": request, "atab": "overview", "tab": "overview",
            "units": units, "channels": channels, "deepseek_key_set": deepseek_key_set,
            "ai_rows": ai_rows, "backup_list": backup_list,
            "map_listings_count": map_listings_count,
            "sale_hourly": {"labels": [h["h"].strftime("%H:00") for h in sale_hourly],
                             "counts": [h["cnt"] for h in sale_hourly]},
            "rental_hourly": {"labels": [h["h"].strftime("%H:00") for h in rental_hourly],
                               "counts": [h["cnt"] for h in rental_hourly]},
        })

    @app.get("/admin/analytics/{listing_id}", response_class=HTMLResponse)
    async def analytics_detail(request: Request, listing_id: str):
        # Старая отдельная страница объявления — слита с попапом на главной
        # карте (см. /admin/listing/{id} и _render_dashboard выше). Раньше
        # это была самостоятельная (и админ-гейченная) страница с анализом
        # аналогов/торга — теперь тот же контент есть в самом попапе
        # (/admin/api/listing/{id} уже отдаёт bargain/comps), так что вторая
        # версия страницы была чистым дублированием. 301, а не 404 — старые
        # ссылки/закладки продолжают вести на актуальную карточку.
        return RedirectResponse(url=f"/admin/listing/{listing_id}", status_code=301)

    async def _analytics_detail_inner(request: Request, listing_id: str):
        from bot.db.pg import fetchrow as pg_fetchrow, fetch as pg_fetch
        from bot.core.bargain import get_comparables, analyze_bargain

        row = await pg_fetchrow(
            "SELECT * FROM apartment_listings WHERE id = $1", listing_id
        )
        if not row:
            return HTMLResponse("<h2>Not found</h2>", status_code=404)

        listing = dict(row)

        # Свежие аналоги (гексагон+кольцо+класс ЖК — см. bot/core/bargain.py)
        comps, comps_meta = await get_comparables(
            lat=float(listing["lat"]) if listing.get("lat") is not None else None,
            lon=float(listing["lon"]) if listing.get("lon") is not None else None,
            rooms=listing.get("rooms"),
            area=listing.get("area"),
            current_price=listing.get("price", 0),
            complex_name=listing.get("complex_name"),
            district=listing.get("district"),
            exclude_id=listing_id,
        )
        bargain = analyze_bargain(listing.get("price", 0), comps, listing.get("is_owner"), meta=comps_meta)

        # Аренда рядом
        # БАГ (найден): apartment_listings.district = "Есильский р-н"
        # (первая часть адреса как есть), rental_listings.district = "Есиль"
        # (короткий корень из regex в rental_parser). ILIKE-подстрока между
        # "Есильский р-н" и "Есиль" никогда не совпадала — блок был пуст
        # почти всегда, независимо от реального наличия данных аренды.
        # Фикс: нормализуем район той же регуляркой перед запросом.
        import re as _re_district
        _district_raw = listing.get("district") or ""
        _m_district = _re_district.search(
            r"(Есиль|Алматы|Сарыарка|Нура|Байконур)", _district_raw, _re_district.I)
        district_norm = _m_district.group(1).capitalize() if _m_district else None

        rental_comps = await pg_fetch("""
            SELECT complex_name, district, rooms, price, area
            FROM rental_listings
            WHERE ($1::text IS NULL OR district = $1)
              AND ($2::int IS NULL OR rooms = $2)
              AND price > 0
            ORDER BY found_at DESC
            LIMIT 10
        """, district_norm, listing.get("rooms"))

        # Если по точному району+комнатам пусто — тот же район без фильтра
        # по комнатам (лучше приблизительные соседи, чем "данных нет")
        if not rental_comps and district_norm:
            rental_comps = await pg_fetch("""
                SELECT complex_name, district, rooms, price, area
                FROM rental_listings
                WHERE district = $1 AND price > 0
                ORDER BY found_at DESC
                LIMIT 10
            """, district_norm)

        # Аргументы для торга и вопросы продавцу
        from bot.core.listing_intel import build_negotiation_points, build_seller_questions
        negotiation_points = build_negotiation_points(dict(listing), bargain, len(comps))
        seller_questions = build_seller_questions(dict(listing))

        # Скор первички (JSONB может прийти строкой)
        primary_details = listing.get("primary_score_details")
        if isinstance(primary_details, str):
            try:
                import json as _j3
                primary_details = _j3.loads(primary_details)
            except ValueError:
                primary_details = None

        # Фото (JSONB может прийти строкой)
        photos = listing.get("photos")
        if isinstance(photos, str):
            try:
                import json as _j6
                photos = _j6.loads(photos)
            except ValueError:
                photos = None

        # Гексагон-анализ цены
        hexd = listing.get("hex_details")
        if isinstance(hexd, str):
            try:
                import json as _j5
                hexd = _j5.loads(hexd)
            except ValueError:
                hexd = None

        # Слои локации (JSONB может прийти строкой)
        layers = listing.get("layer_details")
        if isinstance(layers, str):
            try:
                import json as _j2
                layers = _j2.loads(layers)
            except ValueError:
                layers = None

        # AI-анализ (JSONB может прийти строкой)
        ai = listing.get("ai_analysis")
        if isinstance(ai, str):
            try:
                import json as _j
                ai = _j.loads(ai)
            except ValueError:
                ai = None

        # reasons хранится как JSON-строка — парсим здесь (в jinja нет fromjson)
        import json as _json
        reasons_list = []
        raw_reasons = listing.get("reasons")
        if raw_reasons:
            try:
                parsed = _json.loads(raw_reasons) if isinstance(raw_reasons, str) else raw_reasons
                if isinstance(parsed, list):
                    reasons_list = parsed
            except (ValueError, TypeError):
                reasons_list = [str(raw_reasons)]

        # История цены объявления
        ph_rows = await pg_fetch("""
            SELECT old_price, new_price, changed_at
            FROM price_history WHERE listing_id = $1
            ORDER BY changed_at ASC
        """, listing_id)
        price_history = []
        if listing.get("first_seen"):
            price_history.append({
                "at": listing["first_seen"],
                "price": ph_rows[0]["old_price"] if ph_rows else listing.get("price"),
            })
        for r in ph_rows:
            price_history.append({"at": r["changed_at"], "price": r["new_price"]})

        # SVG-путь мини-графика цены (считаем здесь, чтобы не мучить jinja)
        price_chart = None
        pts_prices = [p["price"] for p in price_history if p.get("price")]
        if len(price_history) >= 1 and pts_prices:
            W, H = 560, 120
            mn, mx = min(pts_prices), max(pts_prices)
            span = (mx - mn) or 1
            n = len(price_history)
            step = W / max(n - 1, 1)
            coords = [
                (i * step, H - ((p.get("price") or mn) - mn) / span * (H - 16) - 8)
                for i, p in enumerate(price_history)
            ]
            path = " ".join(
                ("M" if i == 0 else "L") + f"{x:.1f},{y:.1f}"
                for i, (x, y) in enumerate(coords)
            )
            first_p, last_p = pts_prices[0], pts_prices[-1]
            price_chart = {
                "path": path, "dots": coords, "w": W, "h": H,
                "delta": last_p - first_p,
                "delta_pct": round((last_p - first_p) / first_p * 100, 1) if first_p else 0,
            }

        # 10 похожих вариантов — приоритет: тот же ЖК -> тот же/соседний гексагон
        # (~300м) -> просто похожая цена по городу. Общая логика с большим
        # попапом на карте — см. bot/core/listing_intel.compute_similar_listings.
        from bot.core.listing_intel import compute_similar_listings
        similar_listings = await compute_similar_listings(listing, listing_id, limit=10)

        return templates.TemplateResponse(
            "analytics_detail.html",
            {
                "request": request,
                "listing": listing,
                "similar_listings": similar_listings,
                "comps": [dict(r) for r in comps],
                "bargain": bargain,
                "rental_comps": [dict(r) for r in rental_comps],
                "reasons_list": reasons_list,
                "negotiation_points": negotiation_points,
                "seller_questions": seller_questions,
                "ai": ai,
                "layers": layers,
                "hexd": hexd,
                "photos": photos or [],
                "primary_details": primary_details,
                "price_history": price_history,
                "price_chart": price_chart,
            },
        )


    def _sheets_ok(key: str):
        from bot.db import settings as _as
        from datetime import datetime, timezone
        raw = _as.get(key, "")
        if not raw:
            return None
        try:
            ts = datetime.fromisoformat(raw)
            return (datetime.now(timezone.utc) - ts).total_seconds() < 86400
        except ValueError:
            return None

    def _sheets_when(key: str):
        from bot.db import settings as _as
        from datetime import datetime
        raw = _as.get(key, "")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw).strftime("%d.%m %H:%M")
        except ValueError:
            return None

    @app.get("/admin/dashboard/data")
    async def dashboard_data(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        from bot.db import settings as _as
        await _as.load()  # свежие времена синка Sheets

        from bot.db.pg import fetch as pg_fetch, fetchrow as pg_fetchrow, fetchval as pg_fetchval
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        hour_ago = now - timedelta(hours=1)

        result = {}

        # ── Rental ──────────────────────────────────────────────────────────
        try:
            rental_total = await pg_fetchval("SELECT COUNT(*) FROM rental_listings") or 0
            rental_today = await pg_fetchval(
                "SELECT COUNT(*) FROM rental_listings WHERE found_at >= $1", today_start) or 0
            rental_hour = await pg_fetchval(
                "SELECT COUNT(*) FROM rental_listings WHERE found_at >= $1", hour_ago) or 0
            rental_with_complex = await pg_fetchval(
                "SELECT COUNT(*) FROM rental_listings WHERE complex_name IS NOT NULL AND complex_name != ''") or 0
            last_rental = await pg_fetchval(
                "SELECT MAX(found_at) FROM rental_listings")
            last_rental_str = last_rental.strftime("%d.%m %H:%M") if last_rental else None

            # Статус: ок если последняя запись < 2 часов назад
            rental_ok = last_rental and (now - last_rental).total_seconds() < 7200 if last_rental else False

            result["rental"] = {
                "ok": bool(rental_ok),
                "total": rental_total,
                "today": rental_today,
                "hour": rental_hour,
                "with_complex": rental_with_complex,
                "last_parsed": last_rental_str,
                "sheets_ok": _sheets_ok("SHEETS_RENTAL_SYNCED_AT"),
                "sheets_updated": _sheets_when("SHEETS_RENTAL_SYNCED_AT"),
            }
        except Exception as e:
            result["rental"] = {"ok": False, "error": str(e), "total": 0, "today": 0, "hour": 0,
                                 "with_complex": 0, "last_parsed": None, "sheets_ok": False}

        # ── Apartments ──────────────────────────────────────────────────────
        try:
            apt_total = await pg_fetchval("SELECT COUNT(*) FROM apartment_listings") or 0
            apt_today = await pg_fetchval(
                "SELECT COUNT(*) FROM apartment_listings WHERE last_seen >= $1", today_start) or 0
            apt_hour = await pg_fetchval(
                "SELECT COUNT(*) FROM apartment_listings WHERE last_seen >= $1", hour_ago) or 0
            apt_high = await pg_fetchval(
                "SELECT COUNT(*) FROM apartment_listings WHERE score_total >= 70") or 0
            last_apt = await pg_fetchval("SELECT MAX(last_seen) FROM apartment_listings")
            last_apt_str = last_apt.strftime("%d.%m %H:%M") if last_apt else None
            apt_ok = last_apt and (now - last_apt).total_seconds() < 7200 if last_apt else False

            result["apartments"] = {
                "ok": bool(apt_ok),
                "total": apt_total,
                "today": apt_today,
                "hour": apt_hour,
                "high_score_count": apt_high,
                "last_parsed": last_apt_str,
                "sheets_ok": _sheets_ok("SHEETS_APARTMENTS_SYNCED_AT"),
                "sheets_updated": _sheets_when("SHEETS_APARTMENTS_SYNCED_AT"),
            }
        except Exception as e:
            result["apartments"] = {"ok": False, "error": str(e), "total": 0, "today": 0,
                                     "hour": 0, "high_score_count": 0, "last_parsed": None}

        # ── Investment ──────────────────────────────────────────────────────
        try:
            inv_total = await pg_fetchval("SELECT COUNT(*) FROM investment_listings") or 0
            inv_today = await pg_fetchval(
                "SELECT COUNT(*) FROM investment_listings WHERE found_at >= $1", today_start) or 0
            inv_top_score = await pg_fetchval(
                "SELECT MAX(score_total) FROM investment_listings") or 0
            last_inv = await pg_fetchval("SELECT MAX(found_at) FROM investment_listings")
            inv_ok = last_inv and (now - last_inv).total_seconds() < 7200 if last_inv else False

            result["investment"] = {
                "ok": bool(inv_ok),
                "total": inv_total,
                "today": inv_today,
                "top_score": inv_top_score,
                "sheets_ok": None,
                "sheets_updated": None,
            }
        except Exception as e:
            result["investment"] = {"ok": False, "total": 0, "today": 0, "top_score": 0}

        # ── Database ────────────────────────────────────────────────────────
        try:
            rental_idx_count = await pg_fetchval("SELECT COUNT(*) FROM rental_index") or 0
            apt_count = await pg_fetchval("SELECT COUNT(*) FROM apartment_listings") or 0
            inv_count = await pg_fetchval("SELECT COUNT(*) FROM investment_listings") or 0
            dupes = await pg_fetchval(
                "SELECT COUNT(*) FROM apartment_listings WHERE is_duplicate = TRUE") or 0

            result["db"] = {
                "ok": True,
                "rental_index_count": rental_idx_count,
                "apartments_count": apt_count,
                "investment_count": inv_count,
                "dupes_count": dupes,
            }
        except Exception as e:
            result["db"] = {"ok": False, "error": str(e),
                            "rental_index_count": 0, "apartments_count": 0,
                            "investment_count": 0, "dupes_count": 0}

        return JSONResponse(result)


    @app.get("/admin/scoring", response_class=HTMLResponse)
    async def scoring_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("scoring.html", {"request": request})

    @app.get("/admin/logs/page", response_class=HTMLResponse)
    async def logs_full_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("logs_page.html", {"request": request})

    @app.get("/admin/logs/file")
    async def logs_file_data(request: Request, f: str = "web.log", n: int = 200):
        if not is_authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        # Разрешённые файлы
        allowed = {"bot.log", "rental.log", "apartments.log", "web.log",
                   "korter.log", "homsters.log", "market.log"}
        if f not in allowed:
            return JSONResponse({"lines": [], "error": "not allowed"})
        log_path = os.path.join(os.path.dirname(__file__), "..", f)
        log_path = os.path.normpath(log_path)
        try:
            if os.path.exists(log_path):
                with open(log_path, encoding="utf-8", errors="replace") as fh:
                    all_lines = fh.readlines()
                lines = [l.rstrip("\n") for l in all_lines[-n:]]
            else:
                lines = [f"[Файл {f} не найден]"]
        except Exception as e:
            lines = [f"[Ошибка чтения: {e}]"]
        return JSONResponse({"lines": lines})


    @app.get("/admin/complexes", response_class=HTMLResponse)
    async def complexes_page(
        request: Request,
        district: str = "",
        sort: str = "listings",
        search: str = "",
    ):
        # Публичная страница (как главная карта) — админ-элементы скрываются
        # в шаблоне через is_admin(request)
        from bot.db.pg import fetch as pg_fetch

        conditions = []
        params = []
        i = 1

        if district:
            conditions.append(f"c.district ILIKE '%' || ${i} || '%'")
            params.append(district)
            i += 1
        if search:
            conditions.append(f"c.name ILIKE '%' || ${i} || '%'")
            params.append(search)
            i += 1

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        sort_map = {
            "listings": "c.listings_count DESC NULLS LAST",
            "yield": "c.avg_yield DESC NULLS LAST",
            "price": "c.avg_price_m2 DESC NULLS LAST",
            "year": "c.year_built DESC NULLS LAST",
        }
        order = sort_map.get(sort, "c.listings_count DESC NULLS LAST")

        rows = await pg_fetch(
            f"""
            SELECT c.*, d.name as developer_name
            FROM complexes c
            LEFT JOIN developers d ON d.id = c.developer_id
            {where}
            ORDER BY {order}
            LIMIT 3000
            """,
            *params,
        )
        total_all = await pg_fetch(f"SELECT COUNT(*) AS n FROM complexes c {where}", *params)

        def _serialize(r):
            d = dict(r)
            for k, v in d.items():
                if hasattr(v, 'isoformat'):
                    d[k] = v.isoformat()
            return d

        return templates.TemplateResponse(
            "complexes.html",
            {
                "request": request,
                "complexes": [_serialize(r) for r in rows],
                "total": total_all[0]["n"] if total_all else len(rows),
                "shown": len(rows),
                "filters": {"district": district, "sort": sort, "search": search},
            },
        )

    @app.post("/admin/complexes/update")
    async def complexes_update(
        request: Request,
        id: int = Form(...),
        developer_name: str = Form(default=""),
        year_built: str = Form(default=""),
        address: str = Form(default=""),
        has_parking: bool = Form(default=False),
        has_security: bool = Form(default=False),
        has_closed_territory: bool = Form(default=False),
        has_playground: bool = Form(default=False),
        school_distance_m: str = Form(default=""),
        lrt_distance_m: str = Form(default=""),
        notes: str = Form(default=""),
    ):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)

        from bot.db.pg import execute as pg_exec, fetchrow as pg_get

        # Найти или создать застройщика
        dev_id = None
        if developer_name.strip():
            dev = await pg_get(
                "SELECT id FROM developers WHERE name ILIKE $1 OR $1 = ANY(aliases)",
                developer_name.strip()
            )
            if dev:
                dev_id = dev["id"]
            else:
                dev_id = await pg_exec(
                    "INSERT INTO developers (name) VALUES ($1) ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING id",
                    developer_name.strip()
                )

        await pg_exec(
            """
            UPDATE complexes SET
                developer_id = COALESCE($2, developer_id),
                year_built = COALESCE(NULLIF($3, '')::integer, year_built),
                address = COALESCE(NULLIF($4, ''), address),
                has_parking = $5,
                has_security = $6,
                has_closed_territory = $7,
                has_playground = $8,
                school_distance_m = COALESCE(NULLIF($9, '')::integer, school_distance_m),
                lrt_distance_m = COALESCE(NULLIF($10, '')::integer, lrt_distance_m),
                notes = NULLIF($11, ''),
                updated_at = NOW()
            WHERE id = $1
            """,
            id, dev_id,
            year_built.strip() or None,
            address.strip() or None,
            has_parking, has_security, has_closed_territory, has_playground,
            school_distance_m.strip() or None,
            lrt_distance_m.strip() or None,
            notes.strip() or None,
        )

        return RedirectResponse(url="/admin/complexes", status_code=302)


    @app.get("/admin/complex_scores", response_class=HTMLResponse)
    async def complex_scores_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)

        from bot.db.pg import fetch

        rows = await fetch("""
            SELECT complex_name, rooms,
                   round(avg_score, 1) as avg_score,
                   round(median_price/1000000, 1) as price_m,
                   round(yield_pct, 1) as yield_pct,
                   listings_count
            FROM complex_scores
            WHERE yield_pct IS NOT NULL
            ORDER BY yield_pct DESC
            LIMIT 50
        """)

        return templates.TemplateResponse(
            "complex_scores.html",
            {"request": request, "complexes": [dict(r) for r in rows]}
        )

    # Доп. маршруты: настройки-ползунки, монетизация, запуск проекта, топ-10
    from terminal_extras import make_extras_router
    app.include_router(make_extras_router(templates))

    return app
