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
_LOG_FILE = "bot.log"


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
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
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

        conditions = ["score_total IS NOT NULL"]
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

        return templates.TemplateResponse(
            "analytics_detail.html",
            {
                "request": request,
                "listing": listing,
                "comps": [dict(r) for r in comps],
                "bargain": bargain,
                "rental_comps": [dict(r) for r in rental_comps],
            },
        )

    return app
