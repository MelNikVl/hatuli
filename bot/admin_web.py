"""
Admin web panel (FastAPI + Jinja2).

Migrated from krisha_bot/admin_web.py — updated to use bot.db.compat.BotDB
and templates located in bot/templates/.
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import bot.state as _state
from bot.db.compat import BotDB

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
_LOG_FILE = "web.log"


def create_admin_app(db: BotDB, admin_password: str, bot_version: str, db_path: str = "") -> FastAPI:
    app = FastAPI(title="Krisha Bot Admin")
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)

    def is_authed(request: Request) -> bool:
        return request.cookies.get("admin_auth") == "1"

    @app.get("/admin/login", response_class=HTMLResponse)
    async def admin_login_page(request: Request):
        return templates.TemplateResponse("login.html", {"request": request, "error": None})

    @app.post("/admin/login", response_class=HTMLResponse)
    async def admin_login(request: Request, password: str = Form(...)):
        if password != admin_password:
            return templates.TemplateResponse("login.html", {"request": request, "error": "Неверный пароль"})
        response = RedirectResponse(url="/admin", status_code=302)
        response.set_cookie("admin_auth", "1", httponly=True)
        return response

    @app.get("/admin/logout")
    async def admin_logout():
        response = RedirectResponse(url="/admin/login", status_code=302)
        response.delete_cookie("admin_auth")
        return response

    @app.get("/admin", response_class=HTMLResponse)
    async def dashboard(request: Request):
        # Публичная страница: карта и фильтры без логина; админ-элементы
        # скрываются в шаблоне через is_admin(request)
        stats = await db.get_dashboard_stats()
        return templates.TemplateResponse(
            "dashboard.html", {
                "request": request,
                "stats": stats,
                "bot_version": bot_version,
                "parser_enabled": _state.parser_enabled,
                "parse_interval_min": _state.parse_interval_min,
                "parse_interval_max": _state.parse_interval_max,
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
        min_score: int = 60,
        sort: str = "score_total",
        limit: int = 50,
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
            LIMIT {min(limit, 200)}
            """,
            *params,
        )

        # Статистика rental_index
        rental_stats = await pg_fetch("""
            SELECT district, rooms, median_price, sample_count, complex_name
            FROM rental_index
            WHERE prop_type = 'apartment'
            ORDER BY sample_count DESC
            LIMIT 30
        """)

        return templates.TemplateResponse(
            "analytics.html",
            {
                "request": request,
                "listings": [dict(r) for r in rows],
                "rental_stats": [dict(r) for r in rental_stats],
                "filters": {
                    "district": district,
                    "rooms": rooms,
                    "min_score": min_score,
                    "sort": sort,
                    "limit": limit,
                },
                "total": len(rows),
            },
        )

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

        # Свежие аналоги
        comps = await get_comparables(
            district=listing.get("district"),
            rooms=listing.get("rooms"),
            area=listing.get("area"),
            current_price=listing.get("price", 0),
            exclude_id=listing_id,
        )
        bargain = analyze_bargain(listing.get("price", 0), comps, listing.get("is_owner"))

        # Аренда рядом
        rental_comps = await pg_fetch("""
            SELECT complex_name, district, rooms, price, area
            FROM rental_listings
            WHERE ($1::text IS NULL OR district ILIKE '%' || $1 || '%')
              AND ($2::int IS NULL OR rooms = $2)
              AND price > 0
            ORDER BY found_at DESC
            LIMIT 10
        """, listing.get("district"), listing.get("rooms"))

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

        return templates.TemplateResponse(
            "analytics_detail.html",
            {
                "request": request,
                "listing": listing,
                "comps": [dict(r) for r in comps],
                "bargain": bargain,
                "rental_comps": [dict(r) for r in rental_comps],
                "reasons_list": reasons_list,
                "negotiation_points": negotiation_points,
                "seller_questions": seller_questions,
                "ai": ai,
                "layers": layers,
                "hexd": hexd,
                "primary_details": primary_details,
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
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)

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
                "total": len(rows),
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
