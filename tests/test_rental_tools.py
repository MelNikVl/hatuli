"""Isolated rental workspace tests: no main DB, source HTTP or messaging."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
import httpx
from jinja2 import DictLoader
import pytest
import pytest_asyncio

from bot.core import rental_tools as core
from bot.web import rental_tools as web

NOW = datetime(2026, 10, 15, 17, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_main_database(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/hatuli_rental_tools_test")
    monkeypatch.setenv("HATULI_RENTAL_INQUIRIES_PATH", str(tmp_path / "private" / "requests.sqlite3"))

    async def reject(*args, **kwargs):
        raise AssertionError("Rental tools must not connect to the main database")

    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)


def scenario(**extra):
    return {"monthly_rent_a": 250000, "monthly_rent_b": 280000, "horizon_months": 12,
            "waiting_days": 30, "monthly_owner_expenses": 20000, "oneoff_expenses": 100000,
            "refundable_deposit": 250000, **extra}


def inquiry(**extra):
    return {"kind": "owner", "contact": "@Owner_User", "brief": "Нужен разбор цены моей квартиры",
            "consent": True, "website": "", **extra}


def test_manual_calculation_deducts_waiting_and_both_costs_but_not_deposit():
    result = core.calculate_rental_scenario(scenario())
    assert result["scenario_a"]["gross_horizon"] == 3000000
    assert result["scenario_a"]["net_horizon"] == 2660000
    assert result["scenario_b"]["gross_horizon"] == 3080000
    assert result["scenario_b"]["net_horizon"] == 2740000
    assert result["difference"] == 80000 and result["forgone_rent"] == 250000
    assert result["break_even_waiting_days"] == 38.57
    assert result["max_whole_waiting_days"] == 38
    assert result["manual_scenario"] is True and result["deposit_is_income"] is False
    different_deposit = core.calculate_rental_scenario(scenario(refundable_deposit=1000000))
    for field in ("scenario_a", "scenario_b", "difference", "forgone_rent", "break_even_waiting_days"):
        assert different_deposit[field] == result[field]


def test_twelve_months_are_a_separate_manual_scenario_not_scaled_horizon():
    result = core.calculate_rental_scenario(scenario(horizon_months=6))
    assert result["scenario_a"]["net_horizon"] == 1280000
    assert result["scenario_b"]["net_horizon"] == 1180000
    assert result["difference"] == -100000
    assert result["scenario_a"]["net_12_months"] == 2660000
    assert result["scenario_b"]["net_12_months"] == 2740000
    long = core.calculate_rental_scenario(scenario(horizon_months=24, waiting_days=365))
    assert long["scenario_b"]["gross_12_months"] == 0
    assert long["scenario_b"]["net_12_months"] == -340000
    assert long["scenario_b"]["gross_horizon"] > 0


def test_full_horizon_waiting_still_costs_money_and_lower_rent_has_no_break_even():
    result = core.calculate_rental_scenario(scenario(horizon_months=1, waiting_days=30, monthly_rent_b=200000))
    assert result["scenario_b"]["gross_horizon"] == 0
    assert result["scenario_b"]["net_horizon"] == -120000
    assert result["break_even_waiting_days"] is None
    assert result["max_whole_waiting_days"] is None
    equal = core.calculate_rental_scenario(scenario(monthly_rent_b=250000))
    assert equal["break_even_waiting_days"] == 0


def test_maximum_whole_waiting_days_never_rounds_a_loss_into_a_permitted_day():
    payload = scenario(monthly_rent_a=200001, monthly_rent_b=300000, horizon_months=1,
                       waiting_days=10, monthly_owner_expenses=0, oneoff_expenses=0, refundable_deposit=0)
    result = core.calculate_rental_scenario(payload)
    assert result["difference"] == -1
    assert result["break_even_waiting_days"] == 10  # Display approximation only.
    assert result["max_whole_waiting_days"] == 9
    assert core.calculate_rental_scenario({**payload, "waiting_days": 9})["difference"] > 0
    # Avoid cancellation from 1 - 2/3 rounding just below the exact tenth day.
    exact = core.calculate_rental_scenario({**payload, "monthly_rent_a": 200000})
    assert exact["difference"] == 0 and exact["max_whole_waiting_days"] == 10


def test_decimal_money_is_rounded_only_at_public_result_boundary():
    result = core.calculate_rental_scenario(scenario(monthly_rent_a="100.01", monthly_rent_b="100,01",
        horizon_months="1", waiting_days="1", monthly_owner_expenses="0", oneoff_expenses="0", refundable_deposit="0"))
    assert result["scenario_a"]["net_horizon"] == 100.01
    assert result["scenario_b"]["net_horizon"] == 96.68
    assert result["difference"] == -3.33 and result["forgone_rent"] == 3.33


@pytest.mark.parametrize("field", core.SCENARIO_FIELDS)
@pytest.mark.parametrize("value", [True, False, None, "", "NaN", "Infinity", float("nan"), float("inf"), {}, []])
def test_invalid_manual_numbers_are_rejected(field, value):
    with pytest.raises(core.RentalToolsError) as caught:
        core.calculate_rental_scenario(scenario(**{field: value}))
    assert field in caught.value.errors


@pytest.mark.parametrize("change", [
    {"horizon_months": 0}, {"horizon_months": 25}, {"horizon_months": 1.5},
    {"waiting_days": -1}, {"waiting_days": 366}, {"waiting_days": 1.2},
    {"horizon_months": 1, "waiting_days": 31}, {"monthly_rent_a": 0}, {"monthly_rent_b": 10000001},
    {"monthly_owner_expenses": -1}, {"oneoff_expenses": 100000001}, {"refundable_deposit": -1},
    {"monthly_rent_a": "100.001"}, {"monthly_rent_a": "1e9999999"},
])
def test_ranges_and_manual_waiting_horizon_are_enforced(change):
    with pytest.raises(core.RentalToolsError):
        core.calculate_rental_scenario(scenario(**change))


def test_every_scenario_field_requires_explicit_input():
    for field in core.SCENARIO_FIELDS:
        payload = scenario()
        payload.pop(field)
        with pytest.raises(core.RentalToolsError) as caught:
            core.calculate_rental_scenario(payload)
        assert field in caught.value.errors


def rental_estimate():
    return {"monthly_median": 250000, "monthly_low": 230000, "monthly_high": 270000,
            "sample_count": 8, "observed_at": NOW.isoformat(), "oldest_observed_at": (NOW-timedelta(hours=2)).isoformat(),
            "newest_observed_at": (NOW-timedelta(hours=1)).isoformat(), "basis": "same_complex", "asking_prices": True}


def market_offer(**extra):
    return {"id": "PRIVATE-LISTING-ID", "market_type": "secondary", "complex_id": 10,
            "complex_name": "Пример", "rooms": 2, "area": 60, "address": "PRIVATE-ADDRESS",
            "phone": "PRIVATE-PHONE", "description": "PRIVATE-DESCRIPTION", "rental_estimate": rental_estimate(), **extra}


def test_examples_only_publish_fresh_asking_aggregates_and_are_bounded():
    offers = [market_offer(complex_id=index+1, complex_name=f"Пример {index}") for index in range(60)]
    examples = core.rental_examples_from_snapshot({"generated_at": NOW.isoformat(), "offers": offers+[offers[0]]}, now=NOW)
    assert len(examples) == 40
    assert examples[0]["monthly_low"] == 230000 and examples[0]["monthly_high"] == 270000
    assert examples[0]["monthly_median"] == 250000 and examples[0]["sample_count"] == 8
    assert examples[0]["asking_prices"] is True and examples[0]["basis"] == "same_complex"
    assert "PRIVATE" not in json.dumps(examples)
    assert all("id" not in item and "complex_id" not in item for item in examples)


@pytest.mark.parametrize("bad", [
    {"sample_count": 4}, {"monthly_low": 300000}, {"monthly_high": 900000},
    {"asking_prices": False}, {"basis": "city"}, {"monthly_median": float("nan")},
    {"oldest_observed_at": (NOW-timedelta(hours=73)).isoformat()}, {"newest_observed_at": (NOW+timedelta(seconds=1)).isoformat()},
])
def test_examples_suppress_unqualified_or_expired_ranges(bad):
    offer = market_offer(rental_estimate={**rental_estimate(), **bad})
    assert core.rental_examples_from_snapshot({"generated_at": NOW.isoformat(), "offers": [offer]}, now=NOW) == []


@pytest.mark.parametrize("generated", [None, "invalid", (NOW-timedelta(hours=27)).isoformat(), (NOW+timedelta(seconds=1)).isoformat()])
def test_stale_market_snapshot_never_becomes_fresh_example(generated):
    assert core.rental_examples_from_snapshot({"generated_at": generated, "offers": [market_offer()]}, now=NOW) == []


def test_uncommissioned_primary_and_unknown_identity_do_not_receive_examples():
    for change in ({"market_type": "primary", "build_status": "unknown"}, {"complex_id": None}, {"rooms": True}):
        assert core.rental_examples_from_snapshot({"generated_at": NOW.isoformat(), "offers": [market_offer(**change)]}, now=NOW) == []


@pytest.fixture
def app(tmp_path, monkeypatch):
    templates = Jinja2Templates(directory=str(tmp_path))
    templates.env.loader = DictLoader({
        "rental_tools.html": '<input name="form_token" value="{{ form_token }}"><div>{{ scenario_values|tojson }}</div><section>{{ rental_examples|tojson }}</section>',
        "admin_rental_requests.html": '{% for item in requests %}<p>{{ item.reference }} {{ item.contact }} {{ item.brief }} {{ item.status }} {{ item.version }}</p>{% endfor %}',
    })
    application = FastAPI()
    application.state.analysis_form_secret = "test-rental-form-secret"
    application.include_router(web.make_rental_tools_router(templates))
    monkeypatch.setattr(web, "load_market_snapshot", lambda: {"offers": []})
    monkeypatch.setattr(web, "is_admin", lambda request: request.headers.get("x-test-admin") == "yes")
    monkeypatch.setattr(web, "admin_csrf_token", lambda request: "admin-test-token")
    monkeypatch.setattr(web, "validate_admin_csrf", lambda request, token: token == "admin-test-token")
    return application


@pytest_asyncio.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://test"}) as value:
        yield value


async def fields(client, **extra):
    page = await client.get("/rent")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    token = re.search(r'name="form_token" value="([^"]+)"', page.text)[1]
    return {**inquiry(), "form_token": token, **extra}


@pytest.mark.asyncio
async def test_workspace_and_calculator_are_public_and_never_create_inquiry_storage(client):
    page = await client.get("/rent")
    assert page.status_code == 200
    assert "HttpOnly" in page.headers["set-cookie"] and "SameSite=lax" in page.headers["set-cookie"]
    result = await client.post("/api/rent/scenario", json=scenario())
    assert result.status_code == 200 and result.json()["difference"] == 80000
    assert not core.inquiry_path().exists()


@pytest.mark.asyncio
async def test_public_request_acknowledgement_hides_contact_and_admin_has_real_queue(client):
    values = await fields(client, brief='<script>PRIVATE-REQUEST</script>')
    response = await client.post("/rent/request", json=values)
    assert response.status_code == 202
    reference = response.json()["reference"]
    assert response.json() == {"accepted": True, "reference": reference}
    assert "Owner_User" not in response.text and "PRIVATE" not in response.text
    assert not Path(str(core.inquiry_path())+"-wal").exists()
    if os.name == "posix":
        assert core.inquiry_path().stat().st_mode & 0o777 == 0o600
        assert core.inquiry_path().parent.stat().st_mode & 0o777 == 0o700
    denied = await client.get("/admin/rental-requests", cookies={"admin_auth": "1"})
    assert denied.status_code == 302 and "Owner_User" not in denied.text
    admin = await client.get("/admin/rental-requests", headers={"x-test-admin": "yes"})
    assert admin.status_code == 200 and admin.headers["cache-control"] == "no-store"
    assert "@Owner_User" in admin.text and "&lt;script&gt;" in admin.text
    assert "<script>PRIVATE" not in admin.text
    assert (await client.get("/rent/request")).status_code == 405


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"consent": False}, {"contact": "+77000000000"}, {"kind": "other"}, {"brief": ""}, {"brief": "Короткая задача"}, {"contact": "buyer@-example.kz"}])
async def test_invalid_pilot_input_is_not_persisted(client, change):
    result = await client.post("/rent/request", json=await fields(client, **change))
    assert result.status_code == 422 and not core.inquiry_path().exists()


@pytest.mark.asyncio
async def test_csrf_origin_expiry_and_honeypot_create_no_request(client):
    payload = await fields(client)
    bad = await client.post("/rent/request", json={**payload, "form_token": "forged"})
    assert bad.status_code == 403
    external = await client.post("/rent/request", json=payload, headers={"Origin": "https://other.test"})
    assert external.status_code == 403
    nonce = client.cookies.get(web.COOKIE_NAME)
    expired = web._token("test-rental-form-secret", nonce, int(time.time())-3601)
    assert (await client.post("/rent/request", json={**payload, "form_token": expired})).status_code == 403
    trap = await client.post("/rent/request", json={**payload, "website": "bot", "contact": "invalid"})
    assert trap.status_code == 202 and not core.inquiry_path().exists()


@pytest.mark.asyncio
async def test_duplicate_and_rate_limit_are_persisted_atomically(client):
    payload = await fields(client)
    first = await client.post("/rent/request", json=payload)
    duplicate = await client.post("/rent/request", json=payload)
    assert first.status_code == duplicate.status_code == 202 and first.json() == duplicate.json()
    for index in range(2):
        assert (await client.post("/rent/request", json={**payload, "brief": f"Ещё одна задача по сдаче квартиры {index}"})).status_code == 202
    blocked = await client.post("/rent/request", json={**payload, "brief": "Четвёртая задача по сдаче квартиры"})
    assert blocked.status_code == 429 and blocked.headers["retry-after"] == "3600"
    assert len(core.list_rental_requests()) == 3


def test_simultaneous_duplicates_share_one_reference_without_main_database():
    def submit(_):
        return core.save_rental_request(inquiry(), client_identifier="local-test", secret="test-secret", now=NOW)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(submit, range(8)))
    assert len({result["reference"] for result in results}) == 1
    assert len(core.list_rental_requests()) == 1


@pytest.mark.asyncio
async def test_admin_status_changes_require_admin_csrf_and_current_version(client):
    accepted = await client.post("/rent/request", json=await fields(client))
    reference = accepted.json()["reference"]
    path = f"/admin/rental-requests/{reference}/status"
    payload = {"status": "contacted", "version": 1, "csrf_token": "admin-test-token"}
    assert (await client.post(path, json=payload)).status_code == 401
    admin = {"x-test-admin": "yes"}
    assert (await client.post(path, json={**payload, "csrf_token": "fake"}, headers=admin)).status_code == 403
    assert (await client.post(path, json=payload, headers=admin)).status_code == 303
    row = core.list_rental_requests()[0]
    assert row["status"] == "contacted" and row["version"] == 2
    assert (await client.post(path, json=payload, headers=admin)).status_code == 409
    assert (await client.post(path, json={**payload, "status": "done", "version": 2}, headers=admin)).status_code == 303
    assert core.list_rental_requests()[0]["status"] == "done"
    assert (await client.post(f"/admin/rental-requests/{uuid4()}/status", json=payload, headers=admin)).status_code == 404


@pytest.mark.asyncio
async def test_bounded_bodies_duplicate_fields_and_storage_failures_do_not_leak(client, monkeypatch, caplog):
    duplicate = await client.post("/api/rent/scenario", content='{"monthly_rent_a":1,"monthly_rent_a":2}', headers={"Content-Type": "application/json"})
    assert duplicate.status_code == 422
    large = await client.post("/rent/request", content="x"*8193, headers={"Content-Type": "application/json"})
    assert large.status_code == 422
    payload = await fields(client)
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("PRIVATE-DSN-AND-CONTACT")
    monkeypatch.setattr(web, "save_rental_request", fail)
    failed = await client.post("/rent/request", json=payload)
    assert failed.status_code == 503
    assert "PRIVATE" not in failed.text and "PRIVATE" not in caplog.text


def test_private_store_cannot_be_created_under_static(tmp_path):
    with pytest.raises(ValueError):
        core.save_rental_request(inquiry(), client_identifier="local-test", secret="test-secret", path=tmp_path/"static"/"requests.sqlite3")
    assert not (tmp_path/"static").exists()


@pytest.mark.asyncio
async def test_actual_app_templates_and_complete_owner_path_use_no_main_database(monkeypatch):
    from bot.admin_web import create_admin_app
    from tests.test_public_comparison_routes import NoPrivateDB
    now = datetime.now(timezone.utc)
    evidence = rental_estimate()
    evidence.update(observed_at=now.isoformat(), oldest_observed_at=(now-timedelta(hours=2)).isoformat(),
                    newest_observed_at=(now-timedelta(hours=1)).isoformat())
    monkeypatch.setattr(web, "load_market_snapshot", lambda: {"generated_at": now.isoformat(),
        "offers": [market_offer(complex_name='<script>PRIVATE-AD-TAG</script>', rental_estimate=evidence)]})
    application = create_admin_app(NoPrivateDB(), "test-rental-form-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test",
                                 headers={"Origin": "http://test"}) as client:
        page = await client.get("/rent")
        assert page.status_code == 200
        assert 'action="/api/rent/scenario"' in page.text and 'action="/rent/request"' in page.text
        assert '<a href="/rent"' in page.text
        assert '<script>PRIVATE-AD-TAG</script>' not in page.text
        assert "PRIVATE-LISTING-ID" not in page.text and "PRIVATE-ADDRESS" not in page.text
        assert 'id="rent-examples"' in page.text
        assert "налоги" in page.text.lower() and "не прогноз" in page.text
        result = await client.post("/api/rent/scenario", json=scenario())
        assert result.status_code == 200 and result.json()["difference"] == 80000
        token = re.search(r'name="form_token" value="([^"]+)"', page.text)[1]
        accepted = await client.post("/rent/request", json={**inquiry(), "form_token": token})
        assert accepted.status_code == 202 and len(core.list_rental_requests()) == 1
        denied = await client.get("/admin/rental-requests")
        assert denied.status_code == 302 and "Owner_User" not in denied.text
