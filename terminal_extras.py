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
# (label, min, max, step, unit, ГРУППА)
SLIDER_SETTINGS = {
    "DEPOSIT_RATE":      ("Ставка депозита (KZT)", 5, 25, 0.5, "%", "💰 Финансовые допущения"),
    "APPRECIATION_PCT":  ("Ожидаемый рост цены кв.м", 0, 20, 0.5, "%/год", "💰 Финансовые допущения"),
    "MORTGAGE_RATE":     ("Ставка ипотеки", 5, 25, 0.5, "%", "💰 Финансовые допущения"),
    "MORTGAGE_YEARS":    ("Срок ипотеки", 5, 30, 1, "лет", "💰 Финансовые допущения"),
    "MORTGAGE_DOWN_PCT": ("Первоначальный взнос", 10, 50, 5, "%", "💰 Финансовые допущения"),
    "REALTOR_FEE_PCT":   ("Комиссия риелтора", 0, 5, 0.5, "%", "💰 Финансовые допущения"),
    "ALERT_THRESHOLD":   ("Порог скора для алертов", 50, 90, 1, "баллов", "🎯 Скоринг"),
    "HEX_EDGE_M":        ("Гексагон-сетка: ребро (м)", 30, 200, 10, "м", "🎯 Скоринг"),
    "PARSER_MAX_PAGES":  ("Страниц Krisha за цикл (свежие)", 1, 40, 1, "стр.", "🕷 Обход парсера"),
    "DEEP_SWEEP_BATCH":  ("Глубокий обход: доп. страниц за цикл (0=выкл)", 0, 20, 1, "стр.", "🕷 Обход парсера"),
    "DETAIL_FETCH_BATCH": ("Деталей/координат за цикл (полскора+полслучайно)", 2, 40, 1, "шт.", "🕷 Обход парсера"),
    "COORD_BACKFILL_BATCH": ("Добивка координат по ВСЕЙ базе за цикл", 0, 40, 1, "шт.", "🕷 Обход парсера"),
}


def make_extras_router(templates) -> APIRouter:
    router = APIRouter()

    def is_authed(request: Request) -> bool:
        return request.cookies.get("admin_auth") == "1"

    # Доступно во всех шаблонах: {{ is_admin(request) }} — для скрытия
    # админ-элементов на публичных страницах
    templates.env.globals["is_admin"] = is_authed

    # ── Настройки ─────────────────────────────────────────────────────────

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
            })
        sliders = [s for g in groups.values() for s in g]  # обратная совместимость

        return templates.TemplateResponse("settings.html", {
            "request": request,
            "sliders": sliders,
            "slider_groups": groups,
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

    @router.get("/admin/duplicates", response_class=HTMLResponse)
    async def duplicates_page(request: Request):
        """Страница дублей: кто чей дубль, со ссылками."""
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT p.id, p.address, p.price, p.rooms, p.area, p.is_owner,
                   p.lat, p.lon, p.complex_name, COUNT(d.id) AS dup_cnt,
                   json_agg(json_build_object(
                       'id', d.id, 'price', d.price, 'is_owner', d.is_owner,
                       'url', d.url, 'match', COALESCE(d.dup_match, '?')
                       ) ORDER BY d.is_owner DESC NULLS LAST, d.price ASC) AS dups
            FROM apartment_listings p
            JOIN apartment_listings d ON d.duplicate_of = p.id AND d.is_duplicate = TRUE
            GROUP BY p.id
            ORDER BY dup_cnt DESC, p.last_seen DESC NULLS LAST
            LIMIT 300
        """)
        # «Похожие в ЖК»: ОДИН ЖК, одинаковая комнатность и площадь ±3 м²,
        # но это НЕ дубли (разные объекты — разные адреса/фото).
        # Это отвечает на вопрос «2-3 одинаковые квартиры в одном ЖК?»
        similar = await pg_fetch("""
            SELECT lower(trim(complex_name)) AS cx, rooms,
                   round(area / 5) * 5 AS area_bucket,
                   COUNT(*) AS cnt,
                   json_agg(json_build_object(
                       'id', id, 'price', price, 'area', area,
                       'address', address, 'floor', floor, 'is_owner', is_owner
                       ) ORDER BY price ASC) AS items,
                   AVG(lat) AS lat, AVG(lon) AS lon
            FROM apartment_listings
            WHERE is_active IS NOT FALSE
              AND COALESCE(is_duplicate, FALSE) = FALSE
              AND complex_name IS NOT NULL AND btrim(complex_name) != ''
              AND area IS NOT NULL AND rooms IS NOT NULL
            GROUP BY 1, 2, 3
            HAVING COUNT(*) >= 2
            ORDER BY cnt DESC
            LIMIT 60
        """)
        rent_cnt = 0
        try:
            from bot.db.pg import fetchval as pg_fetchval
            rent_cnt = await pg_fetchval(
                "SELECT COUNT(*) FROM rental_listings WHERE is_duplicate = TRUE") or 0
        except Exception:
            pass
        import json as _json2
        out_rows = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("dups"), str):
                d["dups"] = _json2.loads(d["dups"])
            out_rows.append(d)
        out_sim = []
        for r in similar:
            d = dict(r)
            if isinstance(d.get("items"), str):
                d["items"] = _json2.loads(d["items"])
            out_sim.append(d)
        return templates.TemplateResponse("duplicates.html", {
            "request": request,
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
        """Школы/садики/вузы города для отображения на картах."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
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
                   c.avg_price_m2, c.lat AS c_lat, c.lon AS c_lon,
                   g.lat, g.lon, g.avg_score
            FROM complexes c
            LEFT JOIN developers d ON d.id = c.developer_id
            LEFT JOIN LATERAL (
                SELECT AVG(lat) AS lat, AVG(lon) AS lon,
                       AVG(COALESCE(score_total,0) + COALESCE(zone_bonus,0)
                           + COALESCE(layer_bonus,0))
                         FILTER (WHERE is_active IS NOT FALSE) AS avg_score
                FROM apartment_listings al
                WHERE lower(trim(regexp_replace(al.complex_name, '^\\s*(жк|кг)\\.?\\s+', '', 'i')))
                      = lower(trim(regexp_replace(c.name, '^\\s*(жк|кг)\\.?\\s+', '', 'i')))
                  AND al.lat IS NOT NULL
            ) g ON TRUE
            WHERE COALESCE(c.lat, g.lat) IS NOT NULL
            LIMIT 2500
        """)
        return JSONResponse({"complexes": [{
            "id": r["id"], "name": r["name"],
            "year": r["year_built"], "class": r["housing_class"],
            "active": r["active_cnt"], "sold": r["sold_cnt"],
            "developer": r["developer"] or "—",
            "avg_score": round(float(r["avg_score"])) if r["avg_score"] else None,
            "price_m2": round(float(r["avg_price_m2"])) if r["avg_price_m2"] else None,
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

    @router.get("/admin/api/map-points")
    async def map_points(request: Request, type: str = "sale", rooms: str = "",
                         price_min: float = 0, price_max: float = 0,
                         min_score: int = 0, seller: str = ""):
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
            if price_max > 0:
                conds.append(f"r.price <= ${i}"); params.append(int(price_max)); i += 1
            rows = await pg_fetch(f"""
                SELECT r.id, r.url, r.price, r.rooms, r.complex_name, r.district, r.found_at,
                       g.lat, g.lon
                FROM rental_listings r
                LEFT JOIN LATERAL (
                    SELECT AVG(lat) AS lat, AVG(lon) AS lon
                    FROM apartment_listings al
                    WHERE lower(trim(al.complex_name)) = lower(trim(r.complex_name))
                      AND al.lat IS NOT NULL
                ) g ON TRUE
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
                if d["lat"] is not None:
                    lat, lon, binding, jit = float(d["lat"]), float(d["lon"]), "ЖК", 0.0005
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
                })
            return JSONResponse({"points": pts, "mode": "rental",
                                 "count": len(pts), "no_geo": no_geo})

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
        if min_score > 0:
            conds.append(f"AND (COALESCE(score_total,0) + COALESCE(zone_bonus,0) + COALESCE(layer_bonus,0)) >= ${i}")
            params.append(min_score); i += 1
        if seller == "owner":
            conds.append("AND is_owner IS TRUE")
        elif seller == "agent":
            conds.append("AND is_owner IS DISTINCT FROM TRUE")
        rows = await pg_fetch(f"""
            SELECT a.id, a.lat, a.lon, a.price, a.rooms, a.area, a.address,
                   a.complex_name, a.url,
                   EXTRACT(EPOCH FROM (now() - a.first_seen))/86400 AS age_days,
                   (COALESCE(a.score_total,0) + COALESCE(a.zone_bonus,0)
                    + COALESCE(a.layer_bonus,0)) AS eff_score,
                   ph.old_price AS prev_price,
                   ph.changed_at AS price_changed_at
            FROM apartment_listings a
            LEFT JOIN LATERAL (
                SELECT old_price, changed_at FROM price_history h
                WHERE h.listing_id = a.id
                ORDER BY changed_at DESC LIMIT 1
            ) ph ON TRUE
            WHERE a.lat IS NOT NULL AND a.lon IS NOT NULL
              AND a.is_active IS NOT FALSE
              AND COALESCE(a.is_duplicate, FALSE) = FALSE
              AND a.last_seen > now() - interval '14 days'
              {' '.join(conds)}
            ORDER BY eff_score DESC
            LIMIT 2000
        """, *params)
        pts = [{
            "id": r["id"],
            "lat": float(r["lat"]),
            "lon": float(r["lon"]),
            "score": int(r["eff_score"] or 0),
            "price": r["price"], "rooms": r["rooms"], "area": float(r["area"] or 0),
            "address": r["address"] or "", "complex": r["complex_name"] or "",
            "url": r["url"] or "",
            "age": int(r["age_days"] or 0),
            # последняя смена цены (если была) — для попапа на карте
            "prev_price": r["prev_price"],
            "price_changed": r["price_changed_at"].strftime("%d.%m.%Y") if r["price_changed_at"] else None,
            "top": idx < 10,   # топ-10 лучших — сердечки на карте
        } for idx, r in enumerate(rows)]
        resp = {"points": pts, "count": len(pts)}
        if is_authed(request):
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
            "map_points": [dict(r) for r in cx_map_points],
            "live_cnt": (cx_total["live"] or 0) if cx_total else 0,
            "live_no_coords": (cx_total["live_no_coords"] or 0) if cx_total else 0,
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

    # ── Объявления без привязки к ЖК ──────────────────────────────────────

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
            "request": request, "stats": stats,
        })

    @router.get("/admin/api/unbound-points")
    async def unbound_points(request: Request):
        """Активные объявления без ЖК. Координаты свои, иначе центроид района."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch
        rows = await pg_fetch("""
            SELECT id, url, title, price, rooms, area, address, district,
                   lat, lon, first_seen
            FROM apartment_listings
            WHERE is_active IS NOT FALSE
              AND COALESCE(is_duplicate, FALSE) = FALSE
              AND (complex_name IS NULL OR btrim(complex_name) = '')
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
            if d["lat"] is not None:
                lat, lon, binding = float(d["lat"]), float(d["lon"]), "точно"
            else:
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

    @router.post("/admin/rebind")
    async def rebind_listings(request: Request):
        """Привязка объявлений без ЖК тремя стадиями:
        A) по ссылке на карточку ЖК с Крыши (complex_url = complexes.krisha_url)
        B) по названию ЖК из заголовка/адреса против справочника complexes
        C) по геопозиции: ближайший ЖК в радиусе ~350 м от точки объявления
        Возвращает счётчики по каждой стадии."""
        if not is_authed(request):
            return JSONResponse({"error": "auth"}, status_code=401)
        from bot.db.pg import fetch as pg_fetch, execute as pg_exec, fetchval as pg_fv

        await pg_exec("ALTER TABLE complexes ADD COLUMN IF NOT EXISTS krisha_url TEXT")

        # ── A: точная склейка по ссылке на карточку ЖК ────────────────────
        by_url = (await pg_exec("""
            UPDATE apartment_listings al SET complex_name = c.name
            FROM complexes c
            WHERE (al.complex_name IS NULL OR btrim(al.complex_name) = '')
              AND al.complex_url IS NOT NULL AND c.krisha_url IS NOT NULL
              AND al.complex_url = c.krisha_url
        """) or "").split()[-1]

        # ── B: название ЖК в заголовке/адресе ─────────────────────────────
        complexes = await pg_fetch(
            "SELECT name FROM complexes WHERE name IS NOT NULL AND btrim(name) != ''")
        import re as _re
        def _norm(s: str) -> str:
            s = s.lower()
            s = _re.sub(r"^\s*(жк|кг)\.?\s+", "", s)
            s = _re.sub(r"[«»\"']", " ", s)
            return _re.sub(r"\s+", " ", s).strip()

        norm_map = {}
        for c in complexes:
            n = _norm(c["name"])
            if len(n) >= 3:
                norm_map[n] = c["name"]

        rows = await pg_fetch("""
            SELECT id, title, address FROM apartment_listings
            WHERE complex_name IS NULL OR btrim(complex_name) = ''
        """)
        by_text = 0
        for r in rows:
            hay = _norm(" ".join([r["title"] or "", r["address"] or ""]))
            if not hay:
                continue
            hit = None
            for n, canon in norm_map.items():
                if n in hay:
                    hit = canon
                    break
            if hit:
                await pg_exec(
                    "UPDATE apartment_listings SET complex_name = $2 WHERE id = $1",
                    r["id"], hit)
                by_text += 1

        # ── C: геопривязка к ближайшему ЖК (у объявления есть координаты) ─
        # ~0.0045° ≈ 350 м по широте (по долготе в Астане ещё меньше — ок)
        by_geo = (await pg_exec("""
            UPDATE apartment_listings al
            SET complex_name = (
                SELECT c2.name FROM complexes c2
                WHERE c2.lat IS NOT NULL AND c2.lon IS NOT NULL
                ORDER BY (c2.lat - al.lat)^2 + (c2.lon - al.lon)^2
                LIMIT 1)
            WHERE (al.complex_name IS NULL OR btrim(al.complex_name) = '')
              AND al.lat IS NOT NULL AND al.lon IS NOT NULL
              AND (SELECT min((c.lat - al.lat)^2 + (c.lon - al.lon)^2)
                   FROM complexes c WHERE c.lat IS NOT NULL AND c.lon IS NOT NULL)
                  < 2.0e-5
        """) or "").split()[-1]

        left = await pg_fv("""
            SELECT COUNT(*) FROM apartment_listings
            WHERE is_active IS NOT FALSE
              AND (complex_name IS NULL OR btrim(complex_name) = '')
        """) or 0
        logger.info("rebind: by_url=%s by_text=%d by_geo=%s, осталось без ЖК %d",
                    by_url, by_text, by_geo, left)
        return JSONResponse({
            "ok": True,
            "bound": int(by_url or 0) + by_text + int(by_geo or 0),
            "by_url": int(by_url or 0), "by_text": by_text,
            "by_geo": int(by_geo or 0), "left": left,
        })

    return router
