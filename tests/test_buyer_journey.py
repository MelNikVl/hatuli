"""The real web route is private; its projection distinguishes facts and unknowns."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import shutil
import subprocess

import httpx
import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from bot.core import buyer, buyer_journey, site_auth
from bot.core.admin_sessions import AdminSessionMiddleware
from terminal_extras import make_extras_router


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 13, 7, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def prohibit_database_connections(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("Buyer journey tests must never connect to PostgreSQL")

    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    monkeypatch.setattr("bot.core.admin_sessions.load_admin_session", AsyncMock(return_value=None))
    monkeypatch.setattr(site_auth, "get_user_by_session", AsyncMock(return_value=None))
    for name in ("fetch", "fetchrow", "fetchval", "execute"):
        monkeypatch.setattr(buyer_journey.pg, name, reject)


def app():
    application = FastAPI()
    application.add_middleware(AdminSessionMiddleware)
    application.include_router(make_extras_router(Jinja2Templates(directory=str(ROOT / "bot/templates"))))
    return application


def row(id="123456", rooms=2, area=50, **changes):
    value = {"id": id, "url": "https://krisha.kz/a/show/" + id, "rooms": rooms,
             "area": area, "price": 30_000_000, "last_seen": NOW, "is_active": True}
    value.update(changes)
    return value


@pytest.mark.parametrize("change", [
    {"checked_at": "2026-10-13T08:00:00+00:00"},
    {"source_listing_id": "654321"},
    {"source_price": float("nan")},
    {"url": "https://evil.example/a/show/123456"},
])
def test_source_comparison_rejects_future_wrong_and_nonfinite_observations(tmp_path, change):
    item = {"listing_id": "123456", "source_listing_id": "123456",
            "url": "https://krisha.kz/a/show/123456", "checked_at": NOW.isoformat(),
            "source_active": True, "source_price": 29_000_000, **change}
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"schema_version": 1, "records": [item]}), encoding="utf-8")
    assert buyer_journey.observed_source("123456", 30_000_000, now=NOW, path=path) is None


def source_record(at=NOW, price=29_000_000, **changes):
    return {"listing_id": "123456", "source_listing_id": "123456",
            "url": "https://krisha.kz/a/show/123456", "checked_at": at.isoformat(),
            "source_active": True, "source_price": price, **changes}


def source_file(tmp_path, records):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"schema_version": 1, "records": records}), encoding="utf-8")
    return path


def test_stale_unresolved_price_mismatch_keeps_dated_evidence_without_current_availability(tmp_path):
    old = NOW - timedelta(hours=3)
    path = source_file(tmp_path, [source_record(old, source_active=False)])
    result = buyer_journey.observed_source("123456", 30_000_000, now=NOW, path=path)
    assert result["status"] == "historical_unresolved" and result["is_fresh"] is False
    assert result["checked_at"] == old.isoformat() and not result["price_matches"]
    assert result["active"] is None and result["historical_active"] is False


def test_stale_matching_observation_does_not_pretend_current_source_confirmation(tmp_path):
    path = source_file(tmp_path, [source_record(NOW - timedelta(hours=3), price=30_000_000)])
    assert buyer_journey.observed_source("123456", 30_000_000, now=NOW, path=path) is None


@pytest.mark.parametrize("records", [
    [source_record(NOW - timedelta(hours=4)), source_record(NOW - timedelta(hours=3), price=30_000_000)],
    [source_record(NOW - timedelta(hours=3), price=30_000_000), source_record(NOW - timedelta(hours=4))],
])
def test_newer_matching_observation_resolves_old_mismatch_even_when_file_is_unsorted(tmp_path, records):
    path = source_file(tmp_path, records)
    assert buyer_journey.observed_source("123456", 30_000_000, now=NOW, path=path) is None


def test_newest_valid_mismatch_wins_over_older_match_and_future_invalid_record(tmp_path):
    new = NOW - timedelta(minutes=20)
    path = source_file(tmp_path, [source_record(new, price=28_000_000),
        source_record(NOW - timedelta(hours=1), price=30_000_000),
        source_record(NOW + timedelta(hours=1), price=30_000_000)])
    result = buyer_journey.observed_source("123456", 30_000_000, now=NOW, path=path)
    assert result["price"] == 28_000_000 and result["checked_at"] == new.isoformat()
    assert result["is_fresh"] and result["active"] is True


def test_confirmed_source_price_difference_does_not_replace_cached_price(tmp_path):
    item = {"listing_id": "123456", "source_listing_id": "123456",
            "url": "https://krisha.kz/a/show/123456", "checked_at": NOW.isoformat(),
            "source_active": True, "source_price": 29_000_000}
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"schema_version": 1, "records": [item]}), encoding="utf-8")
    result = buyer_journey.observed_source("123456", 30_000_000, now=NOW, path=path)
    assert result["price"] == 29_000_000 and result["stored_price"] == 30_000_000 and not result["price_matches"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cookies", [{}, {"admin_auth": "1"}, {"admin_session": "A" * 43},
                                        {"site_session": "forged-subscriber"}])
async def test_real_endpoint_denies_unverified_sessions_before_any_private_analysis(monkeypatch, cookies):
    build = AsyncMock(side_effect=AssertionError("Private builder must not be called"))
    monkeypatch.setattr(buyer_journey, "build_buyer_journey", build)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://test",
                                cookies=cookies) as client:
        response = await client.get("/admin/api/buyer-journey/123456")
    assert response.status_code == 403 and response.json() == {"error": "restricted"}
    build.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_login_without_manual_full_access_does_not_grant_journey(monkeypatch):
    monkeypatch.setattr(site_auth, "get_user_by_session", AsyncMock(return_value={"user_id": 42, "full_access": False}))
    build = AsyncMock(side_effect=AssertionError("Must not expose secondary analysis"))
    monkeypatch.setattr(buyer_journey, "build_buyer_journey", build)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://test",
                                cookies={"site_session": "verified-telegram-user"}) as client:
        response = await client.get("/admin/api/buyer-journey/123456")
    assert response.status_code == 403
    build.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_full_access_gets_only_the_builder_projection_and_no_store(monkeypatch):
    monkeypatch.setattr(site_auth, "get_user_by_session", AsyncMock(return_value={"user_id": 42, "full_access": True}))
    payload = {"found": True, "publications": {"count": 1}, "alternatives": []}
    build = AsyncMock(return_value=payload)
    monkeypatch.setattr(buyer_journey, "build_buyer_journey", build)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://test",
                                cookies={"site_session": "verified-full-access"}) as client:
        response = await client.get("/admin/api/buyer-journey/123456")
    assert response.status_code == 200 and response.json() == payload
    assert response.headers["cache-control"] == "no-store"
    build.assert_awaited_once_with("123456")


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["abcde", "1234", "123456789012345678901", "12345<script>"])
async def test_authorized_endpoint_rejects_invalid_id_before_builder(monkeypatch, identifier):
    monkeypatch.setattr(site_auth, "get_user_by_session", AsyncMock(return_value={"full_access": True}))
    build = AsyncMock(side_effect=AssertionError("Invalid ID reached builder"))
    monkeypatch.setattr(buyer_journey, "build_buyer_journey", build)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://test",
                                cookies={"site_session": "full-access"}) as client:
        response = await client.get("/admin/api/buyer-journey/" + identifier)
    assert response.status_code == 422 and response.json()["error"] == "invalid_listing_id"
    build.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_has_controlled_response_without_private_exception_details(monkeypatch):
    monkeypatch.setattr(site_auth, "get_user_by_session", AsyncMock(return_value={"full_access": True}))
    monkeypatch.setattr(buyer_journey, "build_buyer_journey", AsyncMock(side_effect=TimeoutError("PRIVATE-TOKEN")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://test",
                                cookies={"site_session": "full-access"}) as client:
        response = await client.get("/admin/api/buyer-journey/123456")
    assert response.status_code == 503 and response.json()["found"] is False
    assert "PRIVATE-TOKEN" not in response.text


def test_differing_rooms_and_area_have_both_values_dates_and_source_links():
    result = buyer_journey.publication_summary([row(), row("123457", rooms=3, area=52)], "provisional")
    differences = {d["field"]: d for d in result["discrepancies"]}
    assert set(differences) == {"rooms", "area"}
    assert [v["value"] for v in differences["rooms"]["observations"]] == [2, 3]
    assert [v["value"] for v in differences["area"]["observations"]] == [50, 52]
    for difference in differences.values():
        assert all(v["observed_at"] == NOW.isoformat() for v in difference["observations"])
        assert all(v["url"].startswith("https://krisha.kz/a/show/") for v in difference["observations"])
    assert result["identity_confirmed"] is False and "требует проверки" in result["message"]


@pytest.mark.parametrize("rows,identity", [([row()], "confirmed"), ([row(), row("123457")], "provisional"),
                                           ([row(), row("123457", rooms=None, area=None)], None)])
def test_missing_evidence_never_claims_no_discrepancies(rows, identity):
    result = buyer_journey.publication_summary(rows, identity)
    assert result["discrepancies"] == []
    assert "расхождений нет" not in result["message"].lower()
    if len(rows) == 1:
        assert "расхождения квартиры не подтверждены" in result["message"]
    if identity != "confirmed":
        assert not result["identity_confirmed"]


def test_identity_confirmation_does_not_remove_source_discrepancies():
    result = buyer_journey.publication_summary([row(), row("123457", area=60)], "confirmed")
    assert result["identity_confirmed"] and len(result["discrepancies"]) == 1


def test_empty_publication_history_does_not_claim_one_known_publication():
    result = buyer_journey.publication_summary([], None)
    assert result["count"] == 0 and result["items"] == []
    assert "одна публикация" not in result["message"]
    assert "не найдена" in result["message"]


@pytest.mark.asyncio
async def test_unknown_web_listing_does_not_import_or_read_publication_links(monkeypatch):
    analyze = AsyncMock(return_value={"found": False, "message": "Неизвестная квартира"})
    monkeypatch.setattr(buyer_journey, "analyze_for_buyer", analyze)
    result = await buyer_journey.build_buyer_journey("123456")
    analyze.assert_awaited_once_with("123456", allow_import=False)
    assert result == {"found": False, "message": "Неизвестная квартира"}


@pytest.mark.asyncio
async def test_no_import_mode_in_actual_analyzer_keeps_unknown_links_readonly(monkeypatch):
    monkeypatch.setattr(buyer, "resolve_listing", AsyncMock(return_value=SimpleNamespace(
        data={"listing_id": "123456", "found": False})))
    imp = AsyncMock(side_effect=AssertionError("Web card must not import an unknown listing"))
    monkeypatch.setattr(buyer, "import_listing", imp)
    result = await buyer.analyze_for_buyer("123456", allow_import=False)
    assert not result["found"] and "пока нет в базе" in result["message"]
    imp.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_projection_caps_alternatives_and_does_not_copy_private_fields(monkeypatch):
    base = row(phone="PRIVATE-PHONE", seller_name="PRIVATE-NAME", address="PRIVATE-ADDRESS", lat=None, lon=None)
    alternatives = [{"listing": row(str(123457 + n), phone="PRIVATE-PHONE", address="PRIVATE-ADDRESS"),
                     "distance_m": 200, "advantages": ["Дешевле"], "category": "better"} for n in range(5)]
    monkeypatch.setattr(buyer_journey, "analyze_for_buyer", AsyncMock(return_value={
        "found": True, "listing": base, "better_nearby": alternatives}))
    monkeypatch.setattr(buyer_journey.pg, "fetch", AsyncMock(return_value=[base]))
    result = await buyer_journey.build_buyer_journey("123456")
    assert len(result["alternatives"]) == 3
    assert "PRIVATE-" not in json.dumps(result, ensure_ascii=False)
    assert result["publications"]["count"] == 1


@pytest.mark.asyncio
async def test_confirmed_price_mismatch_is_first_warning_so_three_item_display_cannot_hide_it(monkeypatch):
    base = row(lat=51.1, lon=71.4)
    source = {"checked_at": NOW.isoformat(), "price": 29_000_000,
              "stored_price": 30_000_000, "price_matches": False, "is_fresh": False,
              "active": None, "historical_active": True}
    monkeypatch.setattr(buyer_journey, "observed_source", lambda *a, **kw: source)
    monkeypatch.setattr(buyer_journey, "analyze_for_buyer", AsyncMock(return_value={
        "found": True, "listing": base, "better_nearby": [{"listing": row("123457"),
            "warnings": ["Неизвестен год", "Неизвестна отделка", "Неизвестен этаж", "Дополнительное замечание"]}]}))
    monkeypatch.setattr(buyer_journey.pg, "fetch", AsyncMock(return_value=[base]))
    result = await buyer_journey.build_buyer_journey("123456")
    warnings = result["alternatives"][0]["warnings"]
    assert "29 000 000" in warnings[0] and "30 000 000" in warnings[0]
    assert "13.10.2026 07:00 UTC" in warnings[0]
    assert "Текущую цену и доступность нужно проверить заново" in warnings[0]
    assert warnings[1] == "Неизвестен год"


@pytest.mark.asyncio
async def test_no_coordinate_explains_empty_alternatives_instead_of_claiming_no_nearby_options(monkeypatch):
    base = row(lat=None, lon=None)
    monkeypatch.setattr(buyer_journey, "analyze_for_buyer", AsyncMock(return_value={
        "found": True, "listing": base, "better_nearby": []}))
    monkeypatch.setattr(buyer_journey.pg, "fetch", AsyncMock(return_value=[]))
    fallback = AsyncMock(return_value=base)
    monkeypatch.setattr(buyer_journey.pg, "fetchrow", fallback)
    result = await buyer_journey.build_buyer_journey("123456")
    assert result["alternatives"] == [] and "Без координаты" in result["alternatives_message"]
    fallback.assert_awaited_once()


def test_journey_javascript_compiles_and_rejects_source_url_and_markup_injections():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the isolated DOM renderer test")
    result = subprocess.run([node, str(ROOT / "tests/buyer_journey_renderer.test.cjs")],
                            cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr or result.stdout
