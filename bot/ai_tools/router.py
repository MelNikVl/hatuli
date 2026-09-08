"""bot/ai_tools/router.py — тонкий FastAPI-роут поверх bot.ai_tools.core /
bot.ai_tools.data_quality / bot.ai_tools.tool_registry. Сам НИЧЕГО не
считает — разбирает query/path-параметры, зовёт core-функцию, заворачивает
результат в envelope (bot.ai_tools.envelope.build_envelope) и отдаёт JSON.
Тот же паттерн, что terminal_extras.py::make_extras_router() — отдельная
фабрика, подключается в bot/admin_web.py через app.include_router().

Доступ: та же admin-cookie проверка (`admin_auth`), что и у остальных
`/admin/api/*` JSON-роутов в этом проекте (bot/admin_web.py::is_authed,
terminal_extras.py::is_authed) — этот API предназначен для доверенных
server-side вызовов (будущий orchestrator-агент HackAlem), не для
анонимных посетителей сайта, поэтому и вызывает core-функции с
tier='admin' (см. bot/ai_tools/core.py докстринги). Смена на отдельный
API-key механизм — естественный следующий шаг, не сделан здесь намеренно
(см. финальный отчёт задачи, п.6).

Ни один роут не пишет в БД — только await на core.py/data_quality.py,
которые сами только читают (см. их докстринги и tests/test_ai_tools_read_only.py)."""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from bot.ai_tools import core
from bot.ai_tools.data_quality import build_data_quality_snapshot
from bot.ai_tools.envelope import build_envelope
from bot.ai_tools.tool_registry import TOOLS


def make_ai_tools_router() -> APIRouter:
    router = APIRouter()

    def is_authed(request: Request) -> bool:
        return request.cookies.get("admin_auth") == "1"

    def _unauthorized() -> JSONResponse:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    @router.get("/admin/api/ai/v1/tools")
    async def list_tools(request: Request):
        """Реестр capabilities (Этап 4) — read-only введение для будущего
        orchestrator-агента, ничего не исполняет."""
        if not is_authed(request):
            return _unauthorized()
        return JSONResponse(jsonable_encoder(TOOLS))

    @router.get("/admin/api/ai/v1/resolve-listing")
    async def resolve_listing_route(request: Request, url_or_id: str = Query(...)):
        if not is_authed(request):
            return _unauthorized()
        result = await core.resolve_listing(url_or_id)
        envelope = build_envelope("listing", result.data.get("listing_id"), result)
        return JSONResponse(jsonable_encoder(envelope))

    @router.get("/admin/api/ai/v1/property/{property_id}/history")
    async def property_history_route(request: Request, property_id: int):
        if not is_authed(request):
            return _unauthorized()
        result = await core.get_property_history(property_id)
        envelope = build_envelope("property", property_id, result)
        return JSONResponse(jsonable_encoder(envelope))

    @router.get("/admin/api/ai/v1/listing/{listing_id}/analysis")
    async def listing_analysis_route(request: Request, listing_id: str):
        if not is_authed(request):
            return _unauthorized()
        result = await core.get_listing_analysis(listing_id)
        envelope = build_envelope("listing", listing_id, result)
        return JSONResponse(jsonable_encoder(envelope))

    @router.get("/admin/api/ai/v1/listing/{listing_id}/risks")
    async def listing_risks_route(request: Request, listing_id: str):
        if not is_authed(request):
            return _unauthorized()
        result = await core.get_listing_risks(listing_id)
        envelope = build_envelope("listing", listing_id, result)
        return JSONResponse(jsonable_encoder(envelope))

    @router.get("/admin/api/ai/v1/complex/{complex_id}/market-profile")
    async def complex_market_profile_route(request: Request, complex_id: int):
        if not is_authed(request):
            return _unauthorized()
        result = await core.get_complex_market_profile(complex_id)
        envelope = build_envelope("complex", complex_id, result)
        return JSONResponse(jsonable_encoder(envelope))

    @router.get("/admin/api/ai/v1/complex/{complex_id}/location-analysis")
    async def location_analysis_route(request: Request, complex_id: int):
        if not is_authed(request):
            return _unauthorized()
        result = await core.get_location_analysis(complex_id)
        envelope = build_envelope("complex", complex_id, result)
        return JSONResponse(jsonable_encoder(envelope))

    @router.get("/admin/api/ai/v1/data-quality-snapshot")
    async def data_quality_snapshot_route(request: Request):
        if not is_authed(request):
            return _unauthorized()
        snapshot = await build_data_quality_snapshot()
        return JSONResponse(jsonable_encoder(snapshot))

    return router
