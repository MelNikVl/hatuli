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
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from bot.db import settings as app_settings
from bot.db.pg import fetch

logger = logging.getLogger(__name__)

# Сервисы, которыми управляет кнопка "Запустить проект".
# Веб-терминал (krisha-web) сюда не входит — он всегда работает.
PROJECT_SERVICES = ["krisha-rental", "krisha-apartments", "krisha-alerts", "krisha-korter", "krisha-homsters", "krisha-market"]

# Настройки, редактируемые ползунками: key -> (подпись, min, max, шаг, единица)
SLIDER_SETTINGS = {
    "DEPOSIT_RATE":      ("Ставка депозита (KZT)", 5, 25, 0.5, "%"),
    "APPRECIATION_PCT":  ("Ожидаемый рост цены кв.м", 0, 20, 0.5, "%/год"),
    "MORTGAGE_RATE":     ("Ставка ипотеки", 5, 25, 0.5, "%"),
    "MORTGAGE_YEARS":    ("Срок ипотеки", 5, 30, 1, "лет"),
    "MORTGAGE_DOWN_PCT": ("Первоначальный взнос", 10, 50, 5, "%"),
    "REALTOR_FEE_PCT":   ("Комиссия риелтора", 0, 5, 0.5, "%"),
    "ALERT_THRESHOLD":   ("Порог скора для алертов", 50, 90, 1, "баллов"),
    "PARSER_MAX_PAGES":  ("Страниц Krisha за цикл", 1, 40, 1, "стр."),
    "DEEP_SWEEP_BATCH":  ("Глубокий обход: доп. страниц за цикл (0=выкл)", 0, 20, 1, "стр."),
    "DETAIL_FETCH_BATCH": ("Деталей/координат за цикл (полскора+полслучайно)", 2, 40, 1, "шт."),
    "COORD_BACKFILL_BATCH": ("Добивка координат по ВСЕЙ базе за цикл", 0, 40, 1, "шт."),
}


def make_extras_router(templates) -> APIRouter:
    router = APIRouter()

    def is_authed(request: Request) -> bool:
        return request.cookies.get("admin_auth") == "1"

    # ── Настройки ─────────────────────────────────────────────────────────

    @router.get("/admin/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        await app_settings.load()
        current = app_settings.all_settings()
        sliders = [
            {
                "key": key, "label": label, "min": mn, "max": mx,
                "step": step, "unit": unit,
                "value": current.get(key, "0"),
            }
            for key, (label, mn, mx, step, unit) in SLIDER_SETTINGS.items()
        ]
        return templates.TemplateResponse("settings.html", {
            "request": request,
            "sliders": sliders,
            "monetization": app_settings.get_bool("MONETIZATION_ENABLED"),
            "ai_analysis": app_settings.get_bool("AI_TEXT_ANALYSIS"),
            "deepseek_key_set": bool(__import__("os").getenv("DEEPSEEK_API_KEY")),
        })

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
                   bargain_rec, bargain_target, is_owner, year_built, last_seen
            FROM apartment_listings
            WHERE score_total IS NOT NULL
              AND COALESCE(is_duplicate, FALSE) = FALSE
              AND is_active IS NOT FALSE
              AND last_seen > now() - interval '14 days'
              AND price >= 500000
              AND COALESCE(yield_pct, 0) <= 100
            ORDER BY (score_total + COALESCE(zone_bonus, 0) + COALESCE(layer_bonus, 0)) DESC,
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

    # ── Детализация парсеров: график добавлений + статистика ─────────────

    @router.get("/admin/parser/sales", response_class=HTMLResponse)
    async def parser_sales(request: Request, days: int = 1):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        days = days if days in (1, 3, 5) else 1
        from bot.db.pg import fetch as pg_fetch, fetchval as pg_fetchval

        hourly = await pg_fetch("""
            SELECT date_trunc('hour', first_seen) AS h, COUNT(*) AS cnt
            FROM apartment_listings
            WHERE first_seen > now() - ($1 || ' days')::interval
            GROUP BY 1 ORDER BY 1
        """, str(days))
        labels = [r["h"].strftime("%d.%m %H:00") for r in hourly]
        values = [r["cnt"] for r in hourly]

        total_active = await pg_fetchval(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND COALESCE(is_duplicate, FALSE) = FALSE") or 0
        today_new = await pg_fetchval(
            "SELECT COUNT(*) FROM apartment_listings WHERE first_seen::date = CURRENT_DATE") or 0
        today_archived = await pg_fetchval(
            "SELECT COUNT(*) FROM apartment_listings WHERE archived_at::date = CURRENT_DATE") or 0
        price_up = await pg_fetchval(
            "SELECT COUNT(DISTINCT listing_id) FROM price_history "
            "WHERE changed_at::date = CURRENT_DATE AND new_price > old_price") or 0
        price_down = await pg_fetchval(
            "SELECT COUNT(DISTINCT listing_id) FROM price_history "
            "WHERE changed_at::date = CURRENT_DATE AND new_price < old_price") or 0

        stats = [
            {"label": "всего в мониторинге (активных)", "value": f"{total_active:,}".replace(",", " ")},
            {"label": "спаршено сегодня", "value": today_new},
            {"label": "ушло в архив сегодня", "value": today_archived, "color": "#f59e0b"},
            {"label": "цена выросла сегодня", "value": price_up, "color": "#ef4444"},
            {"label": "цена снизилась сегодня", "value": price_down, "color": "#16a34a"},
        ]
        return templates.TemplateResponse("parser_detail.html", {
            "request": request, "title": "🏠 Парсер продаж — детализация",
            "days": days, "stats": stats,
            "chart_labels": labels, "chart_values": values,
        })

    @router.get("/admin/parser/rental", response_class=HTMLResponse)
    async def parser_rental(request: Request, days: int = 1):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        days = days if days in (1, 3, 5) else 1
        from bot.db.pg import fetch as pg_fetch, fetchval as pg_fetchval

        hourly = await pg_fetch("""
            SELECT date_trunc('hour', found_at) AS h, COUNT(*) AS cnt
            FROM rental_listings
            WHERE found_at > now() - ($1 || ' days')::interval
            GROUP BY 1 ORDER BY 1
        """, str(days))
        labels = [r["h"].strftime("%d.%m %H:00") for r in hourly]
        values = [r["cnt"] for r in hourly]

        total = await pg_fetchval("SELECT COUNT(*) FROM rental_listings") or 0
        fresh = await pg_fetchval(
            "SELECT COUNT(*) FROM rental_listings WHERE last_seen > now() - interval '3 days'") or 0
        today_new = await pg_fetchval(
            "SELECT COUNT(*) FROM rental_listings WHERE found_at::date = CURRENT_DATE") or 0
        gone = await pg_fetchval("""
            SELECT COUNT(*) FROM rental_listings
            WHERE last_seen::date = CURRENT_DATE - 3
        """) or 0

        stats = [
            {"label": "всего в базе", "value": f"{total:,}".replace(",", " ")},
            {"label": "живых (видели за 3 дня)", "value": f"{fresh:,}".replace(",", " ")},
            {"label": "спаршено сегодня", "value": today_new},
            {"label": "пропало из выдачи (сдано?)", "value": gone, "color": "#f59e0b"},
        ]
        return templates.TemplateResponse("parser_detail.html", {
            "request": request, "title": "🏢 Парсер аренды — детализация",
            "days": days, "stats": stats,
            "chart_labels": labels, "chart_values": values,
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
        """Школы/садики/вузы города для отображения на картах."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch(
            "SELECT kind, name, lat, lon, address FROM city_poi LIMIT 3000")
        return JSONResponse({"poi": [dict(r) for r in rows]})

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
                   COALESCE(d.name,
                            c.source_info->'korter'->>'developer',
                            c.source_info->'homsters'->>'developer') AS developer,
                   g.lat, g.lon
            FROM complexes c
            LEFT JOIN developers d ON d.id = c.developer_id
            JOIN LATERAL (
                SELECT AVG(lat) AS lat, AVG(lon) AS lon
                FROM apartment_listings al
                WHERE al.complex_name ILIKE '%' || c.name || '%' AND al.lat IS NOT NULL
            ) g ON g.lat IS NOT NULL
            LIMIT 1500
        """)
        return JSONResponse({"complexes": [{
            "id": r["id"], "name": r["name"],
            "year": r["year_built"], "class": r["housing_class"],
            "active": r["active_cnt"], "sold": r["sold_cnt"],
            "developer": r["developer"] or "—",
            "lat": float(r["lat"]), "lon": float(r["lon"]),
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

    @router.get("/admin/api/map-points")
    async def map_points(request: Request):
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch, fetchval as pg_fetchval2

        total_active = await pg_fetchval2(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND COALESCE(is_duplicate, FALSE) = FALSE") or 0
        with_coords = await pg_fetchval2(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND COALESCE(is_duplicate, FALSE) = FALSE AND lat IS NOT NULL") or 0
        rows = await pg_fetch("""
            SELECT id, lat, lon, price, rooms, area, address, complex_name,
                   url,
                   EXTRACT(EPOCH FROM (now() - first_seen))/86400 AS age_days,
                   (COALESCE(score_total,0) + COALESCE(zone_bonus,0)
                    + COALESCE(layer_bonus,0)) AS eff_score
            FROM apartment_listings
            WHERE lat IS NOT NULL AND lon IS NOT NULL
              AND is_active IS NOT FALSE
              AND COALESCE(is_duplicate, FALSE) = FALSE
              AND last_seen > now() - interval '14 days'
            ORDER BY eff_score DESC
            LIMIT 2000
        """)
        pts = [{
            "id": r["id"],
            "lat": float(r["lat"]), "lon": float(r["lon"]),
            "score": int(r["eff_score"] or 0),
            "price": r["price"], "rooms": r["rooms"], "area": float(r["area"] or 0),
            "address": r["address"] or "", "complex": r["complex_name"] or "",
            "url": r["url"] or "",
            "age": int(r["age_days"] or 0),
            "top": idx < 10,   # топ-10 лучших — сердечки на карте
        } for idx, r in enumerate(rows)]
        return JSONResponse({
            "points": pts, "count": len(pts),
            "coverage": {"with_coords": with_coords, "total": total_active},
        })

    # ── Скор: полное описание модели (сердце проекта) ────────────────────

    @router.get("/admin/score-explained", response_class=HTMLResponse)
    async def score_explained(request: Request):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        return templates.TemplateResponse("score_explained.html", {"request": request})

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

        from bot.db.pg import fetchval, execute
        if zone_id:  # обновление существующей
            if polygon and len(polygon) >= 3:
                await execute(
                    "UPDATE priority_zones SET name=$2, bonus=$3, color=$4, polygon=$5::jsonb WHERE id=$1",
                    int(zone_id), name, bonus, color, _json.dumps(polygon))
            else:
                await execute(
                    "UPDATE priority_zones SET name=$2, bonus=$3, color=$4 WHERE id=$1",
                    int(zone_id), name, bonus, color)
            logger.info("zone updated: #%s %s (%+d)", zone_id, name, bonus)
            return JSONResponse({"ok": True, "id": int(zone_id)})

        if not polygon or len(polygon) < 3:
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

    @router.get("/admin/complex/{complex_id}", response_class=HTMLResponse)
    async def complex_detail(request: Request, complex_id: int):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import fetchrow
        cx = await fetchrow("""
            SELECT c.*, d.name AS developer_name
            FROM complexes c LEFT JOIN developers d ON d.id = c.developer_id
            WHERE c.id = $1
        """, complex_id)
        if not cx:
            return HTMLResponse("<h2>ЖК не найден</h2>", status_code=404)

        cname = cx["name"]

        # Координаты ЖК = центроид координат его объявлений; адрес = самый
        # частый адрес среди объявлений (в complexes своих координат нет)
        geo = await fetchrow("""
            SELECT AVG(lat) AS lat, AVG(lon) AS lon
            FROM apartment_listings
            WHERE complex_name ILIKE '%' || $1 || '%' AND lat IS NOT NULL
        """, cname)
        addr_row = await fetchrow("""
            SELECT address, COUNT(*) AS cnt FROM apartment_listings
            WHERE complex_name ILIKE '%' || $1 || '%'
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
            WHERE complex_name ILIKE '%' || $1 || '%'
              AND COALESCE(is_duplicate, FALSE) = FALSE
            ORDER BY is_active DESC NULLS FIRST, score_total DESC NULLS LAST
            LIMIT 60
        """, cname)

        rentals = await fetch("""
            SELECT rooms, price, area, found_at
            FROM rental_listings
            WHERE complex_name ILIKE '%' || $1 || '%' AND price > 0
            ORDER BY found_at DESC LIMIT 30
        """, cname)

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
            WHERE complex_name ILIKE '%' || $1 || '%'
              AND COALESCE(is_duplicate, FALSE) = FALSE AND price > 500000
            GROUP BY rooms ORDER BY rooms
        """, cname)

        # Период наблюдения за ЖК (для контекста цифр выше)
        from bot.db.pg import fetchrow as _fetchrow
        obs = await _fetchrow("""
            SELECT MIN(first_seen) AS since, MAX(last_seen) AS until
            FROM apartment_listings
            WHERE complex_name ILIKE '%' || $1 || '%'
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
            WHERE complex_name ILIKE '%' || $1 || '%' AND price > 0
            GROUP BY rooms ORDER BY rooms
        """, cname)

        return templates.TemplateResponse("complex_detail.html", {
            "request": request,
            "cx": dict(cx),
            "geo": {"lat": float(geo["lat"]), "lon": float(geo["lon"])} if geo and geo["lat"] else None,
            "cx_address": addr_row["address"] if addr_row else None,
            "developer": developer,
            "sales": [dict(r) for r in sale_listings],
            "rentals": [dict(r) for r in rentals],
            "stats": [dict(r) for r in stats],
            "rental_stats": [dict(r) for r in rental_stats],
            "obs": dict(obs) if obs else {},
        })

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

    return router
