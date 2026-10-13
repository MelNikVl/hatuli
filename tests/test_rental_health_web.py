"""Admin health access, aggregate SQL scope, and private state projection."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates
import pytest

from bot import rental_health_web as health

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
PREFIX = "RENTAL_CRAWL_V1_APARTMENT"


def sample_settings():
    return {
        PREFIX: json.dumps({"page": 8, "pages_seen": 7, "started_from_first": True,
                            "seen_ids": ["1011234567", "1011234568"], "signatures": ["private-hash"],
                            "expected_last_page": 200, "expected_total": 4000}),
        PREFIX + "_LAST_SUCCESS_AT": (NOW - timedelta(minutes=3)).isoformat(),
        PREFIX + "_COMPLETED_AT": (NOW - timedelta(hours=4)).isoformat(),
        PREFIX + "_DURATION_SEC": "72000", PREFIX + "_LAST_PAGES": "200",
        PREFIX + "_UNIQUE_SEEN": "3950", PREFIX + "_TOTAL": "4000",
        PREFIX + "_COMPLETED_VERIFIED": "1",
        PREFIX + "_ESTIMATED_HOURS": "23.3", PREFIX + "_PAGE_INTERVAL_SAMPLES": "4",
        "RENTAL_SERVICE_REVISION": "73d0704",
        "RENTAL_METRICS_V1_2026-10-13": json.dumps({"new": 4, "updated": 40,
            "pages": 3, "head_pages": 3, "list_requests": 5, "detail_requests": 4,
            "archive_requests": 1, "http_468": 1}),
    }


def sample_rows():
    return [{"prop_type": "apartment", "total_rows": 3000, "active_rows": 2000,
             "clean_active_rows": 1900, "fresh_72h": 400, "fresh_1h": 80,
             "status_mismatches": 2, "with_complex_source_link": 80,
             "last_observed_at": NOW - timedelta(minutes=2)}]


def test_projection_keeps_three_scopes_and_explicit_unknown_market_coverage():
    result = health.project_rental_health(sample_rows(), sample_settings(), now=NOW)
    assert result["market_coverage"] == "unknown"
    assert [item["type"] for item in result["types"]] == ["apartment", "parking", "commercial"]
    apartment = result["types"][0]
    assert apartment["last_completion_verified"] and apartment["last_circle_hours"] == 20
    assert apartment["status"] == "collecting" and apartment["estimated_circle_hours"] == 23.3
    assert apartment["fresh_72h"] == 400 and apartment["with_complex_source_link"] == 80
    assert result["types"][1]["fresh_72h"] == 0
    assert result["daily"][0]["http_468_share_pct"] == 10
    assert not result["daily"][1]["available"]
    assert any("не подтверждённую сдачу" in note for note in result["notes"])


def test_legacy_circle_duration_does_not_become_verified_inventory():
    settings = sample_settings()
    settings.pop(PREFIX + "_UNIQUE_SEEN")
    result = health.project_rental_health(sample_rows(), settings, now=NOW)["types"][0]
    assert not result["last_completion_verified"] and result["last_circle_hours"] is None
    assert result["last_completed_at"] is not None


@pytest.mark.parametrize("updates", [{"_COMPLETED_VERIFIED": "0"}, {"_DURATION_SEC": "0"},
    {"_UNIQUE_SEEN": "2000"}, {"_TOTAL": "0"}, {"_LAST_PAGES": "0"}])
def test_explicit_complete_flag_and_95_percent_coverage_guard_are_required(updates):
    settings = sample_settings()
    settings.update({PREFIX + key: value for key, value in updates.items()})
    item = health.project_rental_health(sample_rows(), settings, now=NOW)["types"][0]
    assert not item["last_completion_verified"]
    assert item["last_circle_hours"] is None


def test_stale_or_future_success_does_not_publish_eta_as_current():
    for success in (NOW - timedelta(hours=2), NOW + timedelta(hours=2)):
        settings = sample_settings()
        settings[PREFIX + "_LAST_SUCCESS_AT"] = success.isoformat()
        item = health.project_rental_health(sample_rows(), settings, now=NOW)["types"][0]
        assert item["status"] == "stale" and item["estimated_circle_hours"] is None


def test_private_ids_secrets_paths_and_unlisted_cycle_fields_never_escape():
    settings = sample_settings()
    settings.update({"ADMIN_PASSWORD": "secret-admin-password",
        "RENTAL_SERVICE_CYCLE_LAST": json.dumps({"at": NOW.isoformat(), "type": "apartment",
            "list_status": "ok", "raw_listing_id": "1019988776", "url": "private-url",
            "password": "sensitive-cycle-password", "stages": {"backfill": {"seconds": 12,
            "status": "ok", "raw_id": "1019988776"}, "private-stage": {"token": "secret-token"}}})})
    serialized = json.dumps(health.project_rental_health(sample_rows(), settings, now=NOW))
    for forbidden in ("1011234567", "1011234568", "private-hash", "secret-admin-password",
                      "1019988776", "sensitive-cycle-password", "secret-token", "private-url"):
        assert forbidden not in serialized
    assert "raw_listing_id" not in serialized and "ADMIN_PASSWORD" not in serialized


@pytest.mark.parametrize("value", [None, "{broken", "[]", "null", '{"page":0}', '{"page":-2}', '{"page":"secret"}'])
def test_malformed_cursor_is_unknown_without_raw_fallback(value):
    cursor = health._safe_cursor(value)
    assert cursor["next_page"] is None
    assert "seen_ids" not in cursor and "signatures" not in cursor


def test_numeric_and_revision_payloads_are_sanitized():
    settings = sample_settings()
    settings[PREFIX + "_DURATION_SEC"] = "nan"
    settings["RENTAL_SERVICE_REVISION"] = "<script>secret</script>"
    settings["RENTAL_METRICS_V1_2026-10-13"] = json.dumps({"new": -9, "http_468": "inf", "password": "x"})
    result = health.project_rental_health(sample_rows(), settings, now=NOW)
    assert result["service_revision"] == "unknown" and not result["types"][0]["last_completion_verified"]
    assert result["daily"][0]["counters"]["new"] == 0
    assert result["daily"][0]["counters"]["http_468"] == 0


def test_gallery_and_thumbnail_counts_are_separate_and_slug_source_links_supported():
    assert "NULLIF(btrim(photo_url), '') IS NOT NULL" in health.AGGREGATE_SQL
    assert "AS with_gallery" in health.AGGREGATE_SQL
    assert "[0-9]+" not in health.AGGREGATE_SQL.split("complex_url ~", 1)[1].split("AS with_complex_source_link", 1)[0]
    assert health._safe_cycle('{"type":[], "password":"secret"}')["type"] is None


@pytest.mark.asyncio
async def test_loader_only_reads_aggregate_counts_and_allowlisted_settings(monkeypatch):
    fetch = AsyncMock(side_effect=[sample_rows(), [{"key": key, "value": value} for key, value in sample_settings().items()]])
    monkeypatch.setattr(health, "fetch", fetch)
    result = await health.load_rental_health(now=NOW)
    aggregate, settings = fetch.await_args_list
    sql, types = aggregate.args
    assert "GROUP BY prop_type" in sql and "city = 'astana'" in sql
    for clause in ("72 hours", "is_duplicate", "dup_needs_review", "duplicate_of", "archived_at IS NULL"):
        assert clause in sql
    assert "SELECT *" not in sql and "phone" not in sql
    assert types == list(health.TYPES)
    assert "WHERE key = ANY" in settings.args[0]
    assert "ADMIN_PASSWORD" not in settings.args[1]
    assert len(result["types"]) == 3


def make_client(monkeypatch, *, admin=False, fail=False):
    app = FastAPI()
    if admin:
        @app.middleware("http")
        async def auth(request, call_next):
            request.state.admin_user = {"id": 1, "username": "test-admin"}
            return await call_next(request)
    templates = Jinja2Templates(directory=str(Path(__file__).parents[1] / "bot" / "templates"))
    templates.env.globals["is_admin"] = health.is_admin
    templates.env.filters["fmt_num"] = lambda value: str(value) if value is not None else "—"
    loader = AsyncMock(side_effect=RuntimeError("private-db-secret") if fail else None,
                       return_value=health.project_rental_health(sample_rows(), sample_settings(), now=NOW))
    monkeypatch.setattr(health, "load_rental_health", loader)
    health.register_rental_health_routes(app, templates)
    return TestClient(app), loader


@pytest.mark.parametrize("cookies", [{}, {"admin_auth": "1", "admin_user": "admin"}])
def test_unauthorized_and_forged_legacy_cookie_cannot_read_health(monkeypatch, cookies):
    client, loader = make_client(monkeypatch)
    for key, value in cookies.items():
        client.cookies.set(key, value)
    api = client.get("/admin/api/rental-health")
    assert api.status_code == 403 and api.headers["cache-control"] == "no-store"
    page = client.get("/admin/rental-health", follow_redirects=False)
    assert page.status_code == 302 and page.headers["location"] == "/admin/login"
    assert page.headers["cache-control"] == "no-store"
    loader.assert_not_awaited()


def test_authenticated_api_and_html_use_no_store_and_existing_admin_style(monkeypatch):
    client, loader = make_client(monkeypatch, admin=True)
    api = client.get("/admin/api/rental-health")
    assert api.status_code == 200 and api.json()["market_coverage"] == "unknown"
    page = client.get("/admin/rental-health")
    assert page.status_code == 200 and "Сбор аренды" in page.text
    assert "Полнота рынка пока не подтверждена" in page.text
    assert 'class="pub-header"' in page.text and 'data-rental-type="apartment"' in page.text
    for response in (api, page):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-robots-tag"] == "noindex, nofollow"
    loader.assert_awaited()


def test_database_failure_is_sanitized_on_both_routes(monkeypatch):
    client, loader = make_client(monkeypatch, admin=True, fail=True)
    for path in ("/admin/api/rental-health", "/admin/rental-health"):
        response = client.get(path)
        assert response.status_code == 503 and response.headers["cache-control"] == "no-store"
        assert "private-db-secret" not in response.text
