"""tests/test_admin_route_smoke.py — задача 2026-08-30, "Admin cleanup /
IA audit". HTTP-смоук по ВСЕМ статическим (без {param} в пути) admin
GET-роутам (bot/admin_web.py + terminal_extras.py) — тот же паттерн, что
tests/test_complex_detail_route.py (реальный ASGI-запрос через httpx,
не вызов функции напрямую). Цель — конкретное требование задачи:
"все admin routes открываются без 500" + regression-guard на будущее
(если кто-то случайно продублирует роут или сломает handler, этот тест
это поймает).

НЕ проверяет содержимое страниц (это не UI-тест) — только то, что
ASGI-приложение вообще способно обработать запрос без 500/необработанного
исключения. 401/302/404 — валидные ответы для некоторых admin/api
эндпоинтов (например list-параметр вроде candidate_id не передан) —
именно поэтому порог "не 500", не "ровно 200"."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
DB_PATH = os.getenv("DB_PATH", "bot.db")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")


@pytest_asyncio.fixture
async def client():
    import httpx
    from bot.db.pg import init_pool, close_pool
    from bot.db.compat import BotDB
    from bot.admin_web import create_admin_app

    await init_pool(DATABASE_URL)
    db = BotDB(DB_PATH)
    await db.init()
    app = create_admin_app(db, ADMIN_PASSWORD, "test", DB_PATH)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 cookies={"admin_auth": "1"}) as c:
        c.app = app  # для test_dead_duplicate_routes_removed_exactly_one_handler
        # ниже — не полагаемся на приватный httpx.AsyncClient._transport.
        yield c
    await close_pool()


# Все статические (без {param}) GET-роуты /admin/... на 2026-08-30 —
# сгенерировано скриптом-обходом bot/admin_web.py + terminal_extras.py по
# @app.get("/admin...")/@router.get("/admin...") декораторам, вручную
# сверено с фактическим route-table приложения (см. отчёт admin IA audit).
# Параметризованные (/admin/complex/{complex_id} и т.п.) — вне этого теста,
# им нужны реальные id, покрыты отдельными тестами (test_complex_detail_
# route.py и др.), не дублируем здесь.
ADMIN_STATIC_ROUTES = [
    "/admin", "/admin-panel", "/admin/admin-info", "/admin/analytics",
    "/admin/analytics/ai", "/admin/analytics/ai-analysis", "/admin/analytics/ai-status",
    "/admin/analytics/ceiling", "/admin/analytics/complexes", "/admin/analytics/demand",
    "/admin/analytics/demolition", "/admin/analytics/floor-performance", "/admin/analytics/floors",
    "/admin/analytics/genplan", "/admin/analytics/geo", "/admin/analytics/heatmaps",
    "/admin/analytics/homeportal", "/admin/analytics/housing-class", "/admin/analytics/hype",
    "/admin/analytics/market-absorption", "/admin/analytics/market-overview",
    "/admin/analytics/news-analysis", "/admin/analytics/overview", "/admin/analytics/parse-monitor",
    "/admin/analytics/photo-analysis", "/admin/analytics/prices", "/admin/analytics/transport",
    "/admin/analytics/views", "/admin/analytics/walkability", "/admin/analytics/year",
    "/admin/archived", "/admin/backup", "/admin/banks", "/admin/complex_scores",
    "/admin/complexes", "/admin/complexes-fix", "/admin/complexes/data-audit",
    "/admin/developer-reviews", "/admin/developers", "/admin/duplicates", "/admin/entity-ids",
    "/admin/houses", "/admin/info", "/admin/investments", "/admin/issues", "/admin/krisha-lookup",
    "/admin/kzk-registry", "/admin/logs", "/admin/logs/page", "/admin/monitoring",
    "/admin/mortgage-calculator", "/admin/news", "/admin/panel", "/admin/parser",
    "/admin/parser/stats", "/admin/parsers", "/admin/property-match-review", "/admin/renovation",
    "/admin/scoring", "/admin/score-explained", "/admin/settings", "/admin/site-users",
    "/admin/subscriptions", "/admin/top10", "/admin/umbrellas", "/admin/unbound",
    "/admin/users", "/admin/users/stats", "/admin/zones",
]


@pytest.mark.asyncio
async def test_all_static_admin_pages_respond_without_500(client):
    failures = []
    for path in ADMIN_STATIC_ROUTES:
        try:
            resp = await client.get(path, follow_redirects=False)
        except Exception as exc:  # необработанное исключение внутри handler'а
            failures.append(f"{path}: raised {type(exc).__name__}: {exc}")
            continue
        if resp.status_code >= 500:
            failures.append(f"{path}: HTTP {resp.status_code}")
    assert not failures, "admin pages returning 5xx/raising:\n" + "\n".join(failures)


@pytest.mark.asyncio
async def test_dead_duplicate_routes_removed_exactly_one_handler(client):
    """Regression-guard для findings admin IA audit (2026-08-30) —
    /admin/analytics/transport раньше матчил ТРИ зарегистрированных
    handler'а (2 в bot/admin_web.py + 1 в terminal_extras.py), /admin/
    analytics/hype и /admin/analytics/news-analysis — ДВА (уже
    консолидированный redirect-stub в admin_web.py + мёртвый оригинал в
    terminal_extras.py). Только ПЕРВЫЙ зарегистрированный когда-либо
    реально отвечал (Starlette матчит по порядку регистрации) — но
    держать заведомо недостижимый второй/третий handler в коде вводит в
    заблуждение при чтении. Здесь проверяем: ровно один handler в route
    table на каждый путь."""
    app = client.app
    from collections import Counter
    counts = Counter(r.path for r in app.routes if getattr(r, "path", None) in (
        "/admin/analytics/transport", "/admin/analytics/hype", "/admin/analytics/news-analysis",
    ))
    assert counts["/admin/analytics/transport"] == 1
    assert counts["/admin/analytics/hype"] == 1
    assert counts["/admin/analytics/news-analysis"] == 1

    # И это именно ожидаемые handler'ы (не случайно оставшийся не тот).
    by_path = {r.path: r.endpoint.__name__ for r in app.routes
               if getattr(r, "path", None) in (
                   "/admin/analytics/transport", "/admin/analytics/hype", "/admin/analytics/news-analysis")}
    assert by_path["/admin/analytics/transport"] == "transport_page"
    assert by_path["/admin/analytics/hype"] == "hype_analytics_page_old"
    assert by_path["/admin/analytics/news-analysis"] == "news_analysis_page"
