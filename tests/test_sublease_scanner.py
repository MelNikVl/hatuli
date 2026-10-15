"""All DB/HTTP access mocked. Public aggregate routes cannot read paid pools."""
from datetime import datetime, timedelta, timezone
import csv
import io
import json
from decimal import Decimal
from pathlib import Path

import asyncpg
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
import httpx
import pytest
import pytest_asyncio

from bot.core import sublease_market as market
from bot.web import rental_scanner as web
from tests.test_sublease import inputs

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/hatuli_stage3_test")
    async def reject(*args, **kwargs):
        raise AssertionError("Stage3 isolated tests must not connect to a database")
    monkeypatch.setattr(asyncpg, "connect", reject)
    monkeypatch.setattr(asyncpg, "create_pool", reject)
    monkeypatch.setattr(market, "fetch", reject)
    async def public(*args):
        return "public"
    monkeypatch.setattr(web, "get_user_tier", public)


@pytest_asyncio.fixture
async def client():
    templates = Jinja2Templates(directory=str(ROOT/"bot/templates"))
    templates.env.globals.update(is_admin=lambda request: False)
    app = FastAPI()
    app.include_router(web.make_rental_scanner_router(templates))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


def index(period="daily", **overrides):
    now = datetime.now(timezone.utc)
    return {"city": "astana", "district": "Есильский", "complex_id": 123, "complex_name": "ЖК",
        "rooms": 2, "prop_type": "apartment", "rent_period": period, "furnishing": None,
        "median_price": 20000 if period == "daily" else 300000,
        "p25_price": 15000 if period == "daily" else 250000,
        "p75_price": 25000 if period == "daily" else 350000,
        "sample_count": 6, "window_days": 30, "policy_version": "daily_v1" if period == "daily" else "3",
        "updated_at": now, "source_oldest_at": now-timedelta(hours=24), "source_newest_at": now,
        "active_supply": 12, **overrides}


def candidate(**overrides):
    return {"id": "12345678", "price": 200000, "rooms": 2, "area": 55, "district": "Есильский",
        "complex_id": 123, "complex_name": "Точный дом", "furnishing": "full", "has_photo": True,
        "lat": 51.13, "lon": 71.43, "monthly_index": index("monthly"), "daily_index": index(),
        "furnished_monthly_index": None, "contact": "private@example.com", "description": "SECRET TEXT",
        "address": "Туран, 12", **overrides}


@pytest.mark.parametrize("change", [{"sample_count": 4}, {"rent_period": "unknown"}, {"policy_version": "2"},
    {"window_days": None}, {"source_oldest_at": None}, {"source_newest_at": None},
    {"median_price": True}, {"p25_price": 50000}, {"complex_id": None, "district": None},
    {"rooms": None}, {"updated_at": datetime.now(timezone.utc)+timedelta(days=1)},
    {"active_supply": 0}, {"active_supply": None}, {"active_supply": True},
    {"source_newest_at": datetime.now(timezone.utc)-timedelta(days=4)}])
def test_index_gate_hides_missing_stale_and_unqualified(change):
    assert market.ready_index(index(**change)) is None


def test_daily_90_day_history_is_allowed_with_fresh_observations_not_fake_monthly():
    row = index(window_days=90, source_oldest_at=datetime.now(timezone.utc)-timedelta(days=80))
    assert market.ready_index(row)["window_days"] == 90
    assert market.ready_index(index("monthly", window_days=90)) is None
    assert market.ready_index(index("monthly", source_oldest_at=datetime.now(timezone.utc)-timedelta(days=4))) is None


@pytest.mark.asyncio
async def test_segment_falls_from_sparse_complex_to_district_without_city(monkeypatch):
    captured = []
    async def fetch(sql, *args):
        captured.append((sql, args))
        return [index(sample_count=4), index(complex_id=None, complex_name=None, sample_count=10)]
    monkeypatch.setattr(market, "fetch", fetch)
    result = await market.read_segment(district="Есильский", complex_id=123, rooms=2, settings={})
    assert result["daily"]["level"] == "district" and result["daily"]["n"] == 10
    assert "i.sample_count" in captured[0][0] and "i.source_oldest_at" in captured[0][0]
    assert "NULLIF(btrim(i.district),'') IS NOT NULL" in captured[0][0]


@pytest.mark.asyncio
async def test_absent_stage2_schema_fails_closed(monkeypatch):
    async def missing(*args):
        raise asyncpg.UndefinedColumnError("migration unavailable")
    monkeypatch.setattr(market, "fetch", missing)
    result = await market.read_segment(district="Есильский", rooms=2, settings={})
    assert result == {"monthly": None, "daily": None, "furnished_monthly": None}
    assert await market.read_candidates({"rooms": 2}, {}) == ([], False)


@pytest.mark.asyncio
async def test_public_page_renders_without_db_and_paid_page_uses_existing_lock(client):
    response = await client.get("/rental/scanner")
    assert response.status_code == 200 and 'data-access-locked="true"' in response.text
    assert "письменное согласие" in response.text and "налоговый режим" in response.text
    assert "sublease-map" not in response.text
    locked = await client.get("/rental/scanner?section=candidates")
    assert locked.status_code == 403 and "Квартиры для субаренды" in locked.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/rental/scanner", "/api/rental/scanner.csv"])
async def test_paid_gate_precedes_candidate_read_and_parameter_validation(client, monkeypatch, path):
    async def reject(*args, **kwargs):
        raise AssertionError("Unauthenticated request must not reach inventory")
    monkeypatch.setattr(market, "read_candidates", reject)
    response = await client.get(path+"?rooms=nan")
    assert response.status_code == 403 and response.json()["access_locked"] is True
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_free_manual_calculation_needs_no_database(client):
    response = await client.post("/api/rental/calc", json=inputs() | {"rent_month": 200000, "nightly": 20000, "furnishing": "none"})
    assert response.status_code == 200 and response.json()["margin"] == 41368
    assert len(response.json()["scenarios"]) == 3


@pytest.mark.asyncio
async def test_free_calc_requires_tax_never_zero_default(client):
    values = inputs() | {"rent_month": 200000, "nightly": 20000}
    values.pop("tax_pct")
    response = await client.post("/api/rental/calc", json=values)
    assert response.status_code == 422 and "tax_pct" in response.json()["errors"]


@pytest.mark.asyncio
async def test_manual_room_setting_selector_rejects_boolean(client):
    response = await client.post("/api/rental/calc", json=inputs() | {"rent_month": 200000, "nightly": 20000, "rooms": True})
    assert response.status_code == 422 and "rooms" in response.json()["errors"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ['{"rent_month":1,"rent_month":2}', '[]', '{"url":"https://example.com"}',
    '{"tax_pct":NaN}', '{"nightly":true}'])
async def test_bad_json_and_nonsense_do_not_become_scenarios(client, body):
    response = await client.post("/api/rental/calc", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_duplicate_query_and_large_body_rejected(client):
    response = await client.get("/api/rental/segment?rooms=1&rooms=2")
    assert response.status_code == 422
    response = await client.post("/api/rental/calc", content="x"*17000, headers={"content-type": "application/json"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_public_segment_is_aggregate_and_costs_remain_missing(client, monkeypatch):
    async def segment(**kwargs):
        return {"monthly": market.ready_index(index("monthly")), "daily": market.ready_index(index()), "furnished_monthly": None}
    monkeypatch.setattr(market, "read_segment", segment)
    response = await client.get("/api/rental/segment?rooms=2&district=Есильский")
    assert response.status_code == 200 and response.json()["economics"] is None
    assert response.json()["price_kind"] == "asking" and response.json()["removal_is_lease"] is False
    assert "items" not in response.json()


@pytest.mark.asyncio
async def test_paid_scanner_and_csv_project_only_allowed_fields(client, monkeypatch):
    async def paid(*args):
        return "subscriber"
    async def rows(*args, **kwargs):
        return [candidate(district="=HYPERLINK(1)")], False
    monkeypatch.setattr(web, "get_user_tier", paid)
    monkeypatch.setattr(market, "read_candidates", rows)
    query = inputs() | {"rooms": 2, "furnishing": "full"}
    response = await client.get("/api/rental/scanner", params=query)
    assert response.status_code == 200 and response.json()["total"] == 1
    item = response.json()["items"][0]
    assert item["economics"]["index_source"]["n"] == 6
    assert item["source_url"] == "https://krisha.kz/a/show/12345678"
    assert item["address"] == "Туран, 12"
    for secret in ("private@example.com", "SECRET TEXT", "description", "contact"):
        assert secret not in response.text
    csv = await client.get("/api/rental/scanner.csv", params=query)
    assert csv.status_code == 200 and "'=HYPERLINK" in csv.text
    assert "private@example.com" not in csv.text


@pytest.mark.asyncio
async def test_negative_scenario_margin_stays_numeric_in_csv(client, monkeypatch):
    async def paid(*args):
        return "subscriber"
    async def rows(*args, **kwargs):
        return [candidate()], False
    monkeypatch.setattr(web, "get_user_tier", paid)
    monkeypatch.setattr(market, "read_candidates", rows)
    response = await client.get("/api/rental/scanner.csv", params=inputs(occupancy=0) | {"rooms": 2})
    row = next(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
    assert int(row["scenario_margin_kzt"]) == -245000


def test_unvalidated_parser_full_label_cannot_zero_furniture_budget():
    result = web._economics(candidate(furnishing="full"), inputs(), {})
    assert result["furnishing"] == "unknown" and result["params_used"]["furnish_capex"] == 600000


@pytest.mark.asyncio
async def test_manual_full_choice_cannot_autozero_unknown_scanner_candidate(client, monkeypatch):
    async def paid(*args):
        return "subscriber"
    async def rows(*args, **kwargs):
        return [candidate(furnishing="unknown")], False
    monkeypatch.setattr(web, "get_user_tier", paid)
    monkeypatch.setattr(market, "read_candidates", rows)
    values = inputs() | {"rooms": 2, "furnishing": "full"}
    values.pop("furnish_capex")  # disabled full-furniture browser control
    response = await client.get("/api/rental/scanner", params=values)
    assert response.status_code == 200 and response.json()["items"] == []
    assert response.json()["unqualified_count"] == 1


def test_scanner_template_initial_sections_hidden_and_percent_inputs():
    template = (ROOT/"bot/templates/rental_scanner.html").read_text(encoding="utf-8")
    assert 'id="sublease-calculation" class="sublease-panel" hidden' in template
    assert 'id="sublease-segment" class="sublease-panel" hidden' in template
    for name in ("occupancy", "tax_pct", "platform_fee_pct", "vacancy_fraction"):
        assert f'name="{name}" data-percent' in template
    assert 'доля 0–1' not in template


@pytest.mark.asyncio
async def test_unknown_index_is_hidden_not_replaced_with_monthly_multiplier(client, monkeypatch):
    async def paid(*args):
        return "admin"
    async def rows(*args, **kwargs):
        return [candidate(daily_index=None)], False
    monkeypatch.setattr(web, "get_user_tier", paid)
    monkeypatch.setattr(market, "read_candidates", rows)
    response = await client.get("/api/rental/scanner", params=inputs() | {"rooms": 2})
    assert response.status_code == 200 and response.json()["items"] == []


@pytest.mark.asyncio
async def test_single_listing_free_calculation_does_not_expose_row(client, monkeypatch):
    async def rows(*args, **kwargs):
        assert kwargs["listing_id"] == "12345678" and kwargs["require_cheap"] is False
        return [candidate()], False
    monkeypatch.setattr(market, "read_candidates", rows)
    response = await client.post("/api/rental/calc", json=inputs() | {"rooms": 2, "listing_id": "12345678"})
    assert response.status_code == 200 and response.json()["index_source"]["rent_period"] == "daily"
    assert "PRIVATE" not in response.text and "description" not in response.text
    assert "Туран" not in response.text and "address" not in response.json()


def test_candidate_sql_has_fresh_monthly_identity_and_noncity_index_gates():
    sql = market.CANDIDATES_SQL
    for token in ("r.rent_period = 'monthly'", "r.is_active IS TRUE", "r.last_seen BETWEEN", "r.duplicate_of",
                  "r.hex_edge_m=$8", "source.cid=canonical.id", "NOT canonical.is_umbrella",
                  "i.sample_count", "i.source_newest_at", "i.policy_version", "LIMIT 501"):
        assert token in sql
    assert "r.price<=monthly.median_price*$12::numeric" in sql
    assert "city='astana'" in sql and "NULLIF(btrim(i.district),'') IS NOT NULL" in sql


@pytest.mark.asyncio
async def test_actual_app_registers_routes_once_and_keeps_paid_rows_closed(monkeypatch):
    from bot.admin_web import create_admin_app
    class NoPrivateDB:
        def __getattr__(self, name):
            raise AssertionError("Scanner page may not read main database: " + name)
    app = create_admin_app(NoPrivateDB(), "isolated-placeholder", "stage3-test")
    paths = ["/rental/scanner", "/api/rental/segment", "/api/rental/calc", "/api/rental/scanner", "/api/rental/scanner.csv"]
    def registered(routes):
        # FastAPI versions either flatten or lazily include APIRouters.
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                yield from registered(original.routes)
            else:
                yield route
    routes = list(registered(app.routes))
    for path in paths:
        assert len([route for route in routes if getattr(route, "path", None) == path]) == 1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/rental/scanner")
        assert page.status_code == 200 and page.text.count('id="main-content"') == 1
        assert 'href="/rent"' in page.text
        assert '>Субаренда</a>' not in page.text
        assert (await client.get("/api/rental/scanner.csv", cookies={"admin_auth": "1"})).status_code == 403


@pytest.mark.asyncio
async def test_midterm_requires_furnished_validation_and_explicit_vacancy(client, monkeypatch):
    async def paid(*args):
        return "subscriber"
    full = index("monthly", furnishing="full", policy_version="furnished_monthly_v1", furnishing_version="reviewed")
    async def rows(*args, **kwargs):
        return [candidate(furnishing="none", furnished_monthly_index=full)], False
    monkeypatch.setattr(web, "get_user_tier", paid)
    monkeypatch.setattr(market, "read_candidates", rows)
    query = inputs() | {"rooms": 2, "strategy": "mid_term"}
    assert (await client.get("/api/rental/scanner", params=query)).status_code == 422
    query.update(vacancy_fraction=.1, turnovers_per_year=2)
    # Missing Stage2 manual-50 receipt/rule hash always hides this strategy.
    response = await client.get("/api/rental/scanner", params=query)
    assert response.status_code == 200 and response.json()["items"] == []
    monkeypatch.setattr(market, "furnished_row_ready", lambda row, config: row.get("furnishing_version") == "reviewed")
    response = await client.get("/api/rental/scanner", params=query)
    assert response.status_code == 200 and len(response.json()["items"]) == 1
    assert response.json()["items"][0]["economics"]["params_used"]["vacancy_fraction"] == .1


@pytest.mark.asyncio
async def test_candidate_pagination_and_export_keep_same_bounded_filter_pool(client, monkeypatch):
    async def paid(*args):
        return "subscriber"
    calls = []
    async def rows(filters, config):
        calls.append(filters)
        return [candidate(id=str(10000000+i), price=200000+i) for i in range(30)], False
    monkeypatch.setattr(web, "get_user_tier", paid)
    monkeypatch.setattr(market, "read_candidates", rows)
    query = inputs() | {"rooms": 2, "page": 2, "district": "Есильский", "max_rent": 250000}
    result = (await client.get("/api/rental/scanner", params=query)).json()
    assert result["page"] == 2 and len(result["items"]) == 5 and result["pool_truncated"] is False
    csv = await client.get("/api/rental/scanner.csv", params=query)
    assert len(csv.text.splitlines()) == 31 and calls[-1] == calls[-2]


@pytest.mark.asyncio
async def test_competition_risk_uses_observed_p75_without_claiming_noise(client, monkeypatch):
    async def paid(*args):
        return "subscriber"
    async def rows(*args, **kwargs):
        return [candidate(competition_p75=10)], False
    monkeypatch.setattr(web, "get_user_tier", paid)
    monkeypatch.setattr(market, "read_candidates", rows)
    result = (await client.get("/api/rental/scanner", params=inputs() | {"rooms": 2})).json()["items"][0]
    assert "high_daily_competition" in result["risk_flags"]
    assert "noise" in result["unassessed_factors"] and "noise_not_assessed" not in result["risk_flags"]
    assert result["risk_assessment_complete"] is False


@pytest.mark.asyncio
async def test_candidate_reader_limits_projection_and_uses_500_plus_sentinel(monkeypatch):
    captured = []
    async def fetch(sql, *args):
        captured.append((sql, args))
        return [candidate(id=str(10000000+i)) for i in range(501)]
    monkeypatch.setattr(market, "fetch", fetch)
    rows, truncated = await market.read_candidates({"rooms": 2}, {})
    assert len(rows) == 500 and truncated is True
    assert captured[0][1][-1] is True and captured[0][1][-2] == Decimal(".95")


@pytest.mark.asyncio
@pytest.mark.parametrize("sort", ["breakeven", "margin", "startup"])
async def test_best_breakeven_outside_first_501_requires_narrowing_without_partial_csv(client, monkeypatch, sort):
    async def paid(*args):
        return "subscriber"
    poor_daily = index(median_price=5000, p25_price=4000, p75_price=6000)
    first_501 = [candidate(id=str(10000000+i), price=100000, daily_index=poor_daily) for i in range(501)]
    best = candidate(id="99999999", price=250000, complex_id=999,
        monthly_index=index("monthly", complex_id=999),
        daily_index=index(complex_id=999, median_price=50000, p25_price=40000, p75_price=60000))
    values = inputs() | {"rooms": 2, "sort": sort}
    assert (web._economics(best, values, web._settings())["breakeven_occupancy"]
            < web._economics(first_501[0], values, web._settings())["breakeven_occupancy"])
    async def fetch(sql, *args):
        # SQL's cheap-rent ordering would put this better financial candidate
        # after its LIMIT501. Only a sufficiently narrow scope enumerates it.
        return [best] if args[2] == 999 else first_501
    monkeypatch.setattr(web, "get_user_tier", paid)
    monkeypatch.setattr(market, "fetch", fetch)
    for path in ("/api/rental/scanner", "/api/rental/scanner.csv"):
        response = await client.get(path, params=values)
        assert response.status_code == 422 and response.json()["error"] == "narrower_filters_required"
        assert "items" not in response.json() and "source_url" not in response.text
        assert response.headers["content-type"].startswith("application/json")
        assert "Уточните район" in response.text
    narrowed = await client.get("/api/rental/scanner", params=values | {"complex_id": 999})
    assert narrowed.status_code == 200 and narrowed.json()["items"][0]["id"] == best["id"]
    assert narrowed.json()["ranking_scope"] == "all_qualified_candidates_matching_filters"


@pytest.mark.parametrize("raw,expected", [(None, None), ("", None),
    ("<b>Туран</b>, 12\nтел. +7 777 123-45-67 owner@example.com", "Туран , 12"),
    ("Туран, 12 owner@example.com https://example.com @owner_user", "Туран, 12"),
    ("Туран, 12 +44 20 1234 5678", "Туран, 12"),
    ("Туран, 12 +77771234567", "Туран, 12")])
def test_paid_source_address_is_plain_bounded_without_contacts(raw, expected):
    assert market.source_address(raw) == expected
    assert len(market.source_address("а"*400)) == 300


@pytest.mark.asyncio
async def test_paid_table_and_csv_receive_sanitized_address_only(client, monkeypatch):
    async def paid(*args):
        return "subscriber"
    async def rows(*args, **kwargs):
        return [candidate(address="Туран, 12 телефон +77771234567 owner@example.com")], False
    monkeypatch.setattr(web, "get_user_tier", paid)
    monkeypatch.setattr(market, "read_candidates", rows)
    response = await client.get("/api/rental/scanner", params=inputs() | {"rooms": 2})
    assert response.json()["items"][0]["address"] == "Туран, 12"
    response = await client.get("/api/rental/scanner.csv", params=inputs() | {"rooms": 2})
    assert '"Туран, 12"' in response.text and "7771234567" not in response.text
    assert "owner@example.com" not in response.text


@pytest.mark.asyncio
async def test_rent_page_offers_scanner_without_extra_main_menu_item():
    from bot.admin_web import create_admin_app
    class NoPrivateDB:
        def __getattr__(self, name):
            raise AssertionError("Public rental page may not read main database: " + name)
    app = create_admin_app(NoPrivateDB(), "isolated-placeholder", "stage3-test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/rent")
    assert response.status_code == 200 and 'href="/rental/scanner"' in response.text
    assert "Рассчитать субаренду" in response.text
    assert '>Субаренда</a>' not in response.text
