"""Public comparison behavior, isolated from live DB, bank HTTP and disk snapshots."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
import json

import httpx
import pytest

from bot.admin_web import create_admin_app
from bot.core.hexgrid import hex_center
from bot.core.market_comparison import build_market_snapshot, load_market_snapshot


class NoPrivateDB:
    def __getattr__(self, name):
        raise AssertionError(f"Public comparison touched a private DB: {name}")


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.inputs = {}
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "form":
            self.forms.append(attrs)
        if tag == "input" and attrs.get("name"):
            self.inputs[attrs["name"]] = attrs


def market_snapshot(now):
    latitude, longitude = hex_center("0:0")
    common = {"complex_id": 10, "complex_name": "<script>UNSAFE-NAME</script>",
              "rooms": 2, "area": 60, "price": 30_000_000, "floor": 3, "floors_total": 10,
              "lat": latitude, "lon": longitude,
              "address": "улица Айнаколь, 62", "phone": "PRIVATE-PHONE", "raw_json": "PRIVATE-RAW"}
    primary = [{**common, "id": index + 1, "source": "bi_group", "status": "available",
                "last_seen_at": now, "build_status": None, "completion_year": 2020} for index in range(5)]
    secondary = [{**common, "id": str(9100000000 + index),
                  "url": f"https://krisha.kz/a/show/{9100000000 + index}", "price": 36_000_000,
                  "last_seen": now, "market_type": "secondary", "is_active": True,
                  "is_duplicate": False, "dup_needs_review": False, "finish_level": "renovated"}
                 for index in range(5)]
    public_detail = {"id": "1000000001", "market_type": "secondary", "price": 32_000_000,
                     "address": "улица Сыганак, 12/1", "comparison_only": True,
                     "public_comparison_listing": True, "phone": "PRIVATE-PHONE", "price_events": []}
    return build_market_snapshot(primary, secondary + [public_detail], now=now)


def deposit_catalog(now):
    offers = []
    for key, host in (("freedom", "bankffin.kz"), ("forte", "bank.forte.kz"),
                      ("bereke", "berekebank.kz"), ("home_credit", "home.kz")):
        offers.append({"id": key + "-12", "bank_key": key, "name": "Тестовый вклад",
                       "status": "verified", "checked_at": now.isoformat(), "currency": "KZT",
                       "term_months": 12, "rate_type": "fixed", "effective_pct": 16.87,
                       "nominal_pct": 15.5, "min_amount": 1000, "max_amount": None,
                       "guarantee_limit": 10_000_000, "deposit_type": "other", "can_withdraw": False, "can_top_up": False,
                       "source_url": f"https://{host}/deposits", "conditions": [], "warnings": []})
    return {"schema_version": 1, "checked_at": now.isoformat(), "offers": offers, "sources": []}


def daily_picks(now):
    return {"items": [{"id": "1000000001", "rooms": 2, "area": 60, "price": 32_000_000,
                       "source_url": "https://krisha.kz/a/show/1000000001", "observed_at": now.isoformat(),
                       "source_checked_at": now.isoformat(), "source_status": "active", "cautions": [],
                       "seller_phone": "PRIVATE-PHONE", "address": "PRIVATE-ADDRESS", "raw_description": "PRIVATE-RAW"}],
            "generated_at": now.isoformat(), "selection_date": now.date().isoformat()}


@pytest.fixture(autouse=True)
def isolated_sources(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("Public comparison must not connect to PostgreSQL")
    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr("bot.public_comparison_routes.load_market_snapshot", lambda: market_snapshot(now))
    monkeypatch.setattr("bot.public_comparison_routes.load_deposit_catalog", lambda: deposit_catalog(now))
    monkeypatch.setattr("bot.public_comparison_routes.load_daily_picks", lambda: daily_picks(now))
    return now


@pytest.fixture
def app():
    return create_admin_app(NoPrivateDB(), "test-form-secret", "test")


@pytest.mark.asyncio
async def test_anonymous_page_uses_public_snapshots_and_no_private_secondary_rows(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 cookies={"admin_auth": "1"}) as client:
        response = await client.get("/compare?hex=0%3A0&scope=city&contact=PRIVATE-QUERY")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "PRIVATE" not in response.text
    assert "9100000000" not in response.text  # Raw secondary scanner rows are aggregate evidence only.
    assert '<script>UNSAFE-NAME</script>' not in response.text
    assert '&lt;script&gt;UNSAFE-NAME&lt;/script&gt;' in response.text
    assert 'href="/admin/analysis-requests"' not in response.text
    assert "Freedom Bank" in response.text and "Home Credit Bank" in response.text
    forms = Forms(response.text).forms
    assert any(form.get("action", "").split("#", 1)[0] == "/compare/strategies/calculate" and form.get("method") == "post" for form in forms)
    assert 'data-mc-calculator' not in response.text
    assert any(form.get("action") == "/compare" and form.get("method") == "get" for form in forms)


@pytest.mark.asyncio
async def test_empty_sources_do_not_invent_rates_or_recommendations(app, monkeypatch):
    monkeypatch.setattr("bot.public_comparison_routes.load_market_snapshot", lambda: load_market_snapshot(Path("nonexistent-comparison-fixture.json")))
    monkeypatch.setattr("bot.public_comparison_routes.load_deposit_catalog", lambda: {"offers": []})
    monkeypatch.setattr("bot.public_comparison_routes.load_daily_picks", lambda: None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/compare")
    assert response.status_code == 200
    assert "Свежие данные ещё не получены" not in response.text
    assert "Пока мало данных" not in response.text
    assert "Для сравнения нужен подтверждённый вклад" not in response.text
    assert 'class="mc-rental-guide' not in response.text
    assert 'class="mc-bank-section"' not in response.text
    assert 'id="mc-initial-calculation"' not in response.text
    assert Forms(response.text).inputs["budget"]["value"] == ""
    assert "Базовая цена первички ниже" not in response.text
    assert "16,87%" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["listing=9100000000", "listing=9999999999", "unit=99999999"])
async def test_unknown_or_nonpublic_listing_is_explicit_not_a_different_offer(app, query):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/compare?" + query)
    assert response.status_code == 400
    assert "Выбранное объявление отсутствует" in response.text
    assert "PRIVATE" not in response.text
    assert 'value="30000000"' not in response.text


@pytest.mark.asyncio
async def test_selected_source_prefills_only_budget_without_inventing_building_readiness(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        primary = await client.get("/compare?unit=1")
        secondary = await client.get("/compare?listing=1000000001")
    assert primary.status_code == secondary.status_code == 200
    assert "wait_months" not in Forms(primary.text).inputs
    assert Forms(primary.text).inputs["budget"]["value"] == "30000000"
    assert Forms(secondary.text).inputs["budget"]["value"] == "32000000"
    assert 'data-mc-calculator' not in primary.text
    assert 'href="/listing/nb-1"' in primary.text
    assert any(getattr(route, "path", None) == "/listing/{listing_id}" for route in app.routes)


@pytest.mark.asyncio
async def test_existing_developer_links_keep_selected_amount_without_manual_scenario(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        primary = await client.get("/compare?listing=newbuild:2&hex=3:-2&scope=ring2")
        manual = await client.get("/compare?listing=&hex=3:-2&scope=ring2")
    assert primary.status_code == manual.status_code == 200
    assert Forms(primary.text).inputs["budget"]["value"] == "30000000"
    assert Forms(primary.text).inputs["hex"]["value"] == "3:-2"
    assert Forms(manual.text).inputs["budget"]["value"] == ""
    assert 'data-mc-calculator' not in manual.text
    assert "В поле цены — пример суммы" not in manual.text
    assert 'id="mc-initial-calculation"' not in manual.text


@pytest.mark.asyncio
async def test_legacy_post_keeps_selected_offer_but_does_not_restore_manual_fields(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/compare/calculate", data={
            "price": "32000000", "repair_cost": "1000000", "monthly_rent": "200000", "mode": "live",
            "hex": "3:-2", "rooms": "3", "area": "80", "scope": "ring2", "listing": "1000000001"})
    assert response.status_code == 200
    assert "Выбранная квартира" in response.text
    inputs = Forms(response.text).inputs
    assert inputs["hex"]["value"] == "3:-2"
    assert 'name="rooms"' in response.text
    assert float(inputs["area"]["value"]) == 80
    assert inputs["scope"]["value"] == "ring2"
    assert inputs["budget"]["value"] == "32000000"
    assert "repair_cost" not in inputs and "annual_growth_pct" not in inputs


@pytest.mark.asyncio
async def test_no_js_form_shows_invalid_numeric_error_and_escapes_input(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/compare/calculate", data={"price": "<script>EVIL</script>", "mode": "live"})
    assert response.status_code == 400
    assert "Цена квартиры: требуется число" in response.text
    assert '<script>EVIL</script>' not in response.text
    assert 'EVIL' not in response.text  # Rejected legacy values never become public form fields.


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {"price": True}, {"price": float("nan")},
                                      {"price": float("inf")}, {"price": 40_000_000, "annual_growth_pct": "nan"},
                                      {"price": 40_000_000, "mode": "unknown"}])
async def test_public_json_rejects_nonfinite_wrong_type_and_bad_modes(app, payload):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/public/compare-investment", content=json.dumps(payload),
                                     headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert "error" in response.json()
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_public_json_formula_same_capital_no_double_interest_or_avoided_rent(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/public/compare-investment", json={
            "price": 40_000_000, "repair_cost": 2_000_000, "buy_cost": 500_000, "monthly_rent": 250_000,
            "annual_owner_cost": 100_000, "wait_months": 4, "annual_growth_pct": 0, "mode": "live",
            "phone": "PRIVATE-PHONE", "address": "PRIVATE-ADDRESS"})
    assert response.status_code == 200
    result = response.json()
    assert result["initial_capital"] == 42_500_000
    assert result["property"]["wealth"] == 38_900_000
    assert result["property"]["avoided_rent"] == 2_000_000
    assert "PRIVATE" not in response.text
    for bank in result["banks"]:
        assert bank["deposit_income"] == 7_169_750
        assert bank["deposit_end_value"] == 49_669_750
        assert bank["deposit_cash_after_rent"] == 46_669_750
        assert bank["property_advantage"] == -7_769_750
        assert bank["future_purchase_surplus"] == 4_169_750


@pytest.mark.asyncio
async def test_investment_wait_vacancy_and_break_even_agree_for_landlord(app):
    payload = {"price": 40_000_000, "repair_cost": 2_000_000, "buy_cost": 500_000,
               "monthly_rent": 250_000, "annual_owner_cost": 100_000, "wait_months": 4,
               "vacancy_months": 2, "annual_growth_pct": 0, "mode": "rent_out"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = (await client.post("/api/public/compare-investment", json=payload)).json()
        payload["annual_growth_pct"] = first["banks"][0]["break_even_growth_pct"]
        at_break_even = (await client.post("/api/public/compare-investment", json=payload)).json()
    assert first["property"]["rent_months"] == 6
    assert first["property"]["gross_rent"] == 1_500_000
    assert first["property"]["wealth"] == 41_400_000
    assert first["property"]["wait_rent_cost"] == first["property"]["avoided_rent"] == 0
    assert first["banks"][0]["deposit_cash_after_rent"] == 49_669_750
    assert abs(at_break_even["banks"][0]["property_advantage"]) <= 25  # Returned threshold rounded to four decimals.


@pytest.mark.asyncio
async def test_stale_bank_terms_are_visible_but_never_used_for_a_yield(app, monkeypatch, isolated_sources):
    catalog = deposit_catalog(isolated_sources - timedelta(hours=49))
    monkeypatch.setattr("bot.public_comparison_routes.load_deposit_catalog", lambda: catalog)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/public/compare-investment", json={"price": 40_000_000})
    assert response.status_code == 200
    for bank in response.json()["banks"]:
        assert not bank["eligible"]
        assert "последние 48 часов" in bank["reason"]
        assert "deposit_income" not in bank and "property_advantage" not in bank


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", [("/api/public/compare-investment", b'{"price":40000000}'),
                                      ("/compare/calculate", b"price=40000000")])
async def test_oversized_body_rejected_by_actual_length_not_a_claimed_header(app, path, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path, content=body + b" " * 12_001,
                                     headers={"content-length": "1"})
    assert response.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [{"hex": "javascript:alert(1)"}, {"area": "nan"},
                                       {"rooms": "1.5"}, {"scope": "unknown"}])
async def test_no_js_post_rejects_bad_market_selection(app, selection):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/compare/calculate", data={"price": "40000000", **selection})
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["hex=javascript:alert(1)", "area=nan", "area=inf", "rooms=0", "scope=secret", "unit=-1"])
async def test_bad_get_selection_fails_before_render(app, query):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/compare?" + query)
    assert response.status_code in (400, 422)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), True])
def test_malformed_snapshot_price_per_metre_fails_closed(tmp_path, invalid):
    data = market_snapshot(datetime.now(timezone.utc))
    data["offers"][0]["price_per_m2"] = invalid
    path = tmp_path / "market.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert load_market_snapshot(path)["offers"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["newbuild:0", "newbuild:2147483648"])
async def test_developer_selector_validates_id_range_after_parsing(app, identifier):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/compare", params={"listing": identifier})
    assert response.status_code == 422
    assert response.json()["error"] == "Неверный номер квартиры"


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["not-a-listing", "newbuild:0", "newbuild:2147483648"])
async def test_invalid_nonempty_form_identifier_never_falls_back_to_another_offer(app, identifier):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/compare/calculate", data={"price": "40000000", "listing": identifier})
    assert response.status_code == 422
    assert response.json()["error"] == "Неверный номер квартиры"


@pytest.mark.asyncio
async def test_unknown_complex_has_honest_public_not_found_response(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/compare?complex=999999")
    assert response.status_code == 404
    assert "Для этого ЖК пока нет свежих данных сравнения" in response.text
    assert 'href="/compare"' in response.text
    assert "PRIVATE" not in response.text


@pytest.mark.asyncio
async def test_source_address_and_all_price_events_visible_in_cards_and_selected_offer(app, monkeypatch, isolated_sources):
    data = market_snapshot(isolated_sources)
    data["offers"][-1]["id"] = "1000000001"
    details = data["listing_details"][0]
    from bot.core.comparison_offer_details import build_offer_details
    details.update(build_offer_details({"price": 32_000_000, "address": '<script>ADDRESS-XSS</script> Сыганак 12/1',
        "price_events": [{"old_price": 35_000_000, "new_price": 34_000_000, "changed_at": isolated_sources-timedelta(days=2)},
                         {"old_price": 34_000_000, "new_price": 32_000_000, "changed_at": isolated_sources-timedelta(days=1)}]},
        "secondary", now=isolated_sources))
    monkeypatch.setattr("bot.public_comparison_routes.load_market_snapshot", lambda: data)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/compare?listing=1000000001&scope=city")
    assert response.status_code == 200
    assert '<script>ADDRESS-XSS</script>' not in response.text
    assert '&lt;script&gt;ADDRESS-XSS&lt;/script&gt;' in response.text
    assert 'data-history-status="consistent"' in response.text
    assert 'Все изменения по датам' in response.text
    assert response.text.count('data-price-history-chart') == 2
    assert '35 000 000' in response.text and '34 000 000' in response.text and '32 000 000' in response.text
    assert response.text.count('class="hatuli-price-chart-event"') == 4  # two events in card and selected offer
    assert 'PRIVATE' not in response.text


@pytest.mark.asyncio
async def test_new_daily_id_uses_market_metadata_before_supplement_refresh(app, monkeypatch, isolated_sources):
    data = market_snapshot(isolated_sources)
    data["listing_details"] = []
    source = dict(data["offers"][-1], id="1000000001", price=32_000_000, address="Туран 10")
    data["offers"].append(source)
    monkeypatch.setattr("bot.public_comparison_routes.load_market_snapshot", lambda: data)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/compare?listing=1000000001")
    assert response.status_code == 200
    assert 'Туран 10' in response.text
    assert 'История изменений цены пока не загружена' not in response.text


@pytest.mark.asyncio
async def test_developer_single_recorded_change_never_invents_previous_price(app, monkeypatch, isolated_sources):
    data = market_snapshot(isolated_sources)
    from bot.core.comparison_offer_details import build_offer_details
    data["offers"][0].update(build_offer_details({"price": 30_000_000, "address": "Ұлы дала, участок 18", "building": "C", "section": "1",
        "price_events": [{"price": 30_000_000, "changed_at": isolated_sources-timedelta(days=1)}]}, "primary", now=isolated_sources))
    monkeypatch.setattr("bot.public_comparison_routes.load_market_snapshot", lambda: data)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/compare?unit=1")
    assert response.status_code == 200
    assert 'data-price-history-chart' in response.text and 'Не сохранено' in response.text
    assert 'Цена до первого зафиксированного изменения не сохранена.' in response.text
    assert 'class="hatuli-price-chart-event"' not in response.text
    assert 'Корпус C' in response.text and 'Секция 1' in response.text
    assert 'Адрес проекта из каталога' in response.text


@pytest.mark.asyncio
async def test_incomplete_history_warning_keeps_individual_events(app, monkeypatch, isolated_sources):
    data = market_snapshot(isolated_sources)
    data["offers"][-1]["id"] = "1000000001"
    from bot.core.comparison_offer_details import build_offer_details
    data["listing_details"][0].update(build_offer_details({"price": 32_000_000,
        "price_events": [{"old_price": 35_000_000, "new_price": 32_000_000, "changed_at": isolated_sources-timedelta(days=2)},
                         {"old_price": 35_000_000, "new_price": 32_000_000, "changed_at": isolated_sources-timedelta(days=1)}]}, "secondary", now=isolated_sources))
    monkeypatch.setattr("bot.public_comparison_routes.load_market_snapshot", lambda: data)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/compare?listing=1000000001&scope=city")
    assert response.status_code == 200
    assert 'data-history-status="incomplete"' in response.text
    assert 'есть пропуски' in response.text
    assert response.text.count('class="hatuli-price-chart-event"') == 4
    assert 'class="hatuli-price-chart-connector"' not in response.text
