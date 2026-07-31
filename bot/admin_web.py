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

    @app.get("/admin", response_class=HTMLResponse)
    async def dashboard(request: Request):
        # Публичная страница: карта и фильтры без логина; админ-элементы
        # скрываются в шаблоне через is_admin(request)
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
            }
        )

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
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("logs.html", {"request": request})

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
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        errors = await db.get_parse_errors(50)
        return templates.TemplateResponse("issues.html", {"request": request, "errors": errors})

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
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import fetchval as pg_fv
        total_active = await pg_fv(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND COALESCE(is_duplicate, FALSE) = FALSE") or 0
        missing_floor = await pg_fv(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND COALESCE(is_duplicate, FALSE) = FALSE AND floor IS NULL") or 0
        return templates.TemplateResponse("floors_analytics.html", {
            "request": request, "atab": "floors",
            "total_active": total_active, "missing_floor": missing_floor,
        })

    @app.get("/admin/analytics/ceiling", response_class=HTMLResponse)
    async def ceiling_analytics_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import fetchval as pg_fv
        total_active = await pg_fv(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND COALESCE(is_duplicate, FALSE) = FALSE") or 0
        missing_ceiling = await pg_fv(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND COALESCE(is_duplicate, FALSE) = FALSE AND ceiling_height IS NULL") or 0
        return templates.TemplateResponse("ceiling_analytics.html", {
            "request": request, "atab": "ceiling",
            "total_active": total_active, "missing_ceiling": missing_ceiling,
        })

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

    @app.get("/admin/analytics/floor-performance", response_class=HTMLResponse)
    async def floor_performance_page(request: Request):
        # ВАЖНО: тот же паттерн, что views/floors — этот роут ДОЛЖЕН стоять
        # выше catch-all /admin/analytics/{listing_id} ниже, иначе "floor-
        # performance" матчится туда как несуществующий listing_id ("Not found").
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("floor_performance.html", {
            "request": request, "atab": "floor_performance",
        })

    @app.get("/admin/analytics/hype", response_class=HTMLResponse)
    async def hype_analytics_page(request: Request):
        # ВАЖНО: ДОЛЖЕН стоять выше catch-all /admin/analytics/{listing_id}.
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("hype_analytics.html", {
            "request": request, "atab": "hype",
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

    @app.get("/admin/analytics/{listing_id}", response_class=HTMLResponse)
    async def analytics_detail(request: Request, listing_id: str):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        try:
            return await _analytics_detail_inner(request, listing_id)
        except Exception:
            import traceback
            tb = traceback.format_exc()
            return HTMLResponse(
                f"<h2>Ошибка карточки {listing_id}</h2><pre style='background:#111;color:#f88;"
                f"padding:16px;border-radius:8px;overflow:auto;'>{tb}</pre>",
                status_code=500,
            )

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
        # (~300м) -> просто похожая цена по городу. Меньше 10 — показываем сколько есть.
        similar_listings = []
        if listing.get("rooms") and listing.get("price"):
            price_lo, price_hi = int(listing["price"] * 0.75), int(listing["price"] * 1.25)
            sim_rows = []

            if listing.get("complex_name"):
                sim_rows = list(await pg_fetch("""
                    SELECT id, url, price, area, floor, floors_total, district,
                           complex_name, photos, lat, lon
                    FROM apartment_listings
                    WHERE rooms = $1 AND is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
                      AND id != $2 AND lower(trim(complex_name)) = lower(trim($3))
                    ORDER BY ABS(price - $4) ASC
                    LIMIT 10
                """, listing["rooms"], listing_id, listing["complex_name"], listing["price"]))

            if listing.get("lat") and listing.get("lon"):
                # БАГ (найден, критично): раньше последний fallback здесь не
                # имел вообще никакого гео-ограничения (просто ближайшая цена
                # по всей БД), а потом — ограничение по district ILIKE, но
                # текстовое совпадение района не гарантирует близость (нашли
                # 2 объявления с district="Сарайшык р-н", но геокод у них
                # улетел за сотни км от Астаны — см. fix в rebind.py). Теперь
                # ВСЕГДА только гео: тот же гексагон (300м) + кольцо 1 + кольцо 2
                # — никогда не расширяемся на весь город, даже если найдётся
                # меньше 10 вариантов.
                from bot.core.hexgrid import hex_id as _hex_id, neighbors as _hex_nb
                HEX_EDGE = 300.0
                my_hid = _hex_id(float(listing["lat"]), float(listing["lon"]), HEX_EDGE)
                ring1 = set(_hex_nb(my_hid))
                ring2 = set()
                for h in ring1:
                    ring2.update(_hex_nb(h))
                wanted_hids = {my_hid} | ring1 | ring2
                exclude_ids = [listing_id] + [r["id"] for r in sim_rows]
                candidates = await pg_fetch("""
                    SELECT id, url, price, area, floor, floors_total, district,
                           complex_name, photos, lat, lon
                    FROM apartment_listings
                    WHERE rooms = $1 AND is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
                      AND lat IS NOT NULL AND NOT (id = ANY($2::text[]))
                    ORDER BY ABS(price - $3) ASC
                    LIMIT 1500
                """, listing["rooms"], exclude_ids, listing["price"])
                for c in candidates:
                    if len(sim_rows) >= 10:
                        break
                    if _hex_id(float(c["lat"]), float(c["lon"]), HEX_EDGE) in wanted_hids:
                        sim_rows.append(c)

            for r in sim_rows[:10]:
                sp = r["photos"]
                if isinstance(sp, str):
                    try:
                        sp = _json.loads(sp)
                    except ValueError:
                        sp = []
                similar_listings.append({
                    "id": r["id"], "url": r["url"], "price": r["price"],
                    "area": float(r["area"]) if r["area"] else None,
                    "floor": r["floor"], "floors_total": r["floors_total"],
                    "district": r["district"], "complex_name": r["complex_name"],
                    "photo": (sp or [None])[0],
                    "lat": float(r["lat"]) if r["lat"] else None,
                    "lon": float(r["lon"]) if r["lon"] else None,
                })

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
            LIMIT 200
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
