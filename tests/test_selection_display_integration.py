"""Public reasons/financial thresholds use snapshots, never request-time DBs."""
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest

from bot.admin_web import create_admin_app
from bot.core.comparison_offer_details import build_offer_details
from bot.core.daily_picks_display import prepare_daily_picks
from bot.core.apartment_selection_reasons import best_apartment_reasons
from bot.public_comparison_routes import comparison_context
from tests.test_daily_picks_public_page import snapshot as daily_snapshot
from tests.test_public_comparison_routes import NoPrivateDB, market_snapshot, deposit_catalog, daily_picks


NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def no_private_database_or_forecast_io(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("Selection integration tests must not access PostgreSQL")
    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    monkeypatch.setattr("bot.core.daily_picks_display.forecast_for_offer",
                        lambda *args, **kwargs: {"status": "insufficient_history"})
    monkeypatch.setattr("bot.public_comparison_routes.forecast_for_offer",
                        lambda *args, **kwargs: {"status": "insufficient_history"})


def detailed_market(now, identifier="1000000001", price=32_000_000, area=60):
    market = market_snapshot(now)
    detail = market["listing_details"][0]
    detail.update(id=identifier, area=area, current_price=price)
    detail.update(build_offer_details({"price": price, "area": area, "address": "Сыганак 12/1",
        "ceiling_height": 3, "kitchen_area": 15,
        "price_events": [{"old_price": price+2_000_000, "new_price": price+1_000_000, "changed_at": now-timedelta(days=20)},
                         {"old_price": price+1_000_000, "new_price": price, "changed_at": now-timedelta(days=3)}]}, "secondary", now=now))
    return market


def full_deposits(now):
    catalog = deposit_catalog(now)
    annual = catalog["offers"][0]
    catalog["offers"].append({**annual, "id": "freedom-strategy-3", "name": "Стратегия", "term_months": 3,
                             "effective_pct": 19.5, "nominal_pct": 18.09})
    return catalog


@pytest.fixture
def checked_daily_comparison(monkeypatch):
    edition = daily_snapshot(NOW)
    item = edition["items"][0]
    item["price_comparison"].update(discount_pct=0, reference_price_per_m2=item["price_per_m2"])
    item["selection_facts"].update(is_owner=True, owner_source_verified=True)
    item["source_facts"] = {"is_pledged": True, "contact": "PRIVATE-PLEDGE-CONTACT"}
    item["cautions"] = ["Уточните, есть ли комиссия посредника.", "Состояние и ремонт уточните у продавца."]
    item["reasons"] = best_apartment_reasons(item, NOW)
    assert len(item["reasons"]) == 3
    market = detailed_market(NOW, item["id"], item["price"], item["area"])
    detail = market["listing_details"][0]
    detail["selection_facts"] = {"ceiling_height": None, "kitchen_area": None,
        "finish_level": "finished", "is_owner": False, "owner_source_verified": False}
    detail["price_history"] = build_offer_details({"price": item["price"], "price_events": []},
                                                 "secondary", now=NOW)["price_history"]
    monkeypatch.setattr("bot.public_comparison_routes.load_daily_picks", lambda: edition)
    monkeypatch.setattr("bot.public_comparison_routes.load_market_snapshot", lambda: market)
    monkeypatch.setattr("bot.public_comparison_routes.load_deposit_catalog", lambda: full_deposits(NOW))
    return edition, market, item


@pytest.mark.asyncio
async def test_checked_daily_offer_keeps_three_factors_floor_and_dated_pledge_on_compare(checked_daily_comparison):
    edition, market, item = checked_daily_comparison
    frozen = copy.deepcopy(edition)
    selected = comparison_context(listing=item["id"])["selected_offer"]
    assert selected["is_daily_pick"] is True
    assert selected["reasons"] == item["reasons"]
    assert any("хозяин" in reason for reason in selected["reasons"])
    assert any("этаж" in reason for reason in selected["reasons"])
    assert selected["floor"] == item["floor"] and selected["floors_total"] == item["floors_total"]
    assert selected["selection_facts"] == item["selection_facts"]
    assert selected["complex_identity"] == item["complex_identity"]
    assert selected["source_checked_at"] == item["source_checked_at"]
    assert selected["source_facts"] == {"is_pledged": True}
    assert selected["cautions"] == []
    assert "Krisha.kz" in selected["evidence_notices"][0]
    assert selected["observed_at_label"] in selected["evidence_notices"][0]
    app = create_admin_app(NoPrivateDB(), "test-form-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/compare?listing={item['id']}")
    assert response.status_code == 200
    assert response.text.count("data-daily-picks-factor") == 3
    assert 'data-mc-daily-floor>4 из 12 этаж</p>' in response.text
    assert "В залоге — указано на Krisha.kz при проверке" in response.text
    assert "Уточните, есть ли комиссия посредника." not in response.text
    assert "Состояние и ремонт уточните у продавца." not in response.text
    assert "PRIVATE-PLEDGE-CONTACT" not in response.text
    assert edition == frozen


@pytest.mark.parametrize("change", ["price", "area"])
def test_changed_daily_offer_is_not_redeclared_as_three_supported_recommendations(checked_daily_comparison, change):
    edition, market, item = checked_daily_comparison
    frozen = copy.deepcopy(edition)
    detail = market["listing_details"][0]
    detail["current_price" if change == "price" else "area"] += 1_000_000 if change == "price" else 10
    selected = comparison_context(listing=item["id"])["selected_offer"]
    assert selected["from_daily_picks"] is True and selected["is_daily_pick"] is False
    assert len(selected["reasons"]) < 3
    assert not any("ниже похожих" in reason for reason in selected["reasons"])
    assert selected["floor"] is None
    assert selected["cautions"] == []
    assert selected["source_facts"] == {"is_pledged": True}
    assert len(selected["evidence_notices"]) == 2
    assert any("отличается" in notice or "изменились" in notice for notice in selected["evidence_notices"])
    assert edition == frozen


@pytest.mark.asyncio
async def test_daily_compare_retains_specific_history_gap_and_never_promotes_it_to_a_benefit(checked_daily_comparison):
    edition, market, item = checked_daily_comparison
    price = item["price"]
    market["listing_details"][0]["price_history"] = build_offer_details({"price": price, "price_events": [
        {"old_price": price+2_000_000, "new_price": price+1_000_000, "changed_at": NOW-timedelta(days=2)},
        {"old_price": price+500_000, "new_price": price, "changed_at": NOW-timedelta(days=1)},
    ]}, "secondary", now=NOW)["price_history"]
    selected = comparison_context(listing=item["id"])["selected_offer"]
    assert selected["reasons"] == item["reasons"]
    assert selected["price_history"]["status"] == "incomplete"
    app = create_admin_app(NoPrivateDB(), "test-form-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/compare?listing={item['id']}")
    assert response.status_code == 200
    assert response.text.count("data-daily-picks-factor") == 3
    assert 'data-history-status="incomplete"' in response.text
    assert "Между записанными изменениями цены есть пропуски" in response.text
    assert "В залоге — указано на Krisha.kz при проверке" in response.text


def test_daily_compare_cannot_turn_truthy_unconfirmed_pledge_into_legal_fact(checked_daily_comparison):
    edition, market, item = checked_daily_comparison
    item["source_facts"] = {"is_pledged": "true"}
    selected = comparison_context(listing=item["id"])["selected_offer"]
    assert selected["source_facts"] == {}
    assert not any("залоге" in notice for notice in selected["evidence_notices"])


@pytest.fixture
def public_app(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("Public selection display must not read PostgreSQL")
    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    monkeypatch.setattr("bot.public_comparison_routes.load_market_snapshot", lambda: detailed_market(NOW))
    monkeypatch.setattr("bot.public_comparison_routes.load_deposit_catalog", lambda: full_deposits(NOW))
    def selection():
        picks = daily_picks(NOW)
        picks["items"][0]["price_comparison"] = {"discount_pct": 14.7, "reference_price_per_m2": 625_000,
                                                "scope": "в том же ЖК", "comparable_count": 6}
        return picks
    monkeypatch.setattr("bot.public_comparison_routes.load_daily_picks", selection)
    return create_admin_app(NoPrivateDB(), "test-form-secret", "test")


@pytest.mark.asyncio
async def test_compare_why_shows_price_cash_gap_ceiling_kitchen_and_distinct_cuts(public_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app), base_url="http://test") as client:
        response = await client.get("/compare?listing=1000000001&scope=city")
    assert response.status_code == 200
    assert "Почему в подборке" in response.text
    assert "14,7% ниже" in response.text and "5,5 млн" in response.text
    assert "цену снижали 2 раза" not in response.text
    assert response.text.count('data-price-history-chart') == 1  # selected offer has the recorded history
    assert "2 000 000 ₸" in response.text and "Все изменения по датам" in response.text
    assert "Высокие потолки 3 м" in response.text and "Кухня 15 м²" in response.text
    assert "6 аналогов:" not in response.text
    assert 'class="mc-rental-guide' not in response.text
    assert "PRIVATE" not in response.text


@pytest.mark.asyncio
async def test_no_js_rental_calculator_compares_income_to_short_maximum_without_growth(public_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app), base_url="http://test") as client:
        response = await client.post("/compare/calculate", data={"listing": "1000000001", "price": 32_000_000,
            "mode": "rent_out", "monthly_rent": 500_000, "annual_growth_pct": 50,
            "repair_cost": 0, "buy_cost": 0, "annual_owner_cost": 0, "vacancy_months": 0, "wait_months": 0})
    assert response.status_code == 200
    assert "Доход от аренды после расходов" in response.text
    assert "18,8% за первый год" in response.text
    assert "выше 520 000" in response.text
    assert "В вашем сценарии доходность аренды выше этой ставки." not in response.text
    assert "3 мес." in response.text


def assert_checked_cards_preserved(original, displayed, excluded_ids=()):
    expected = {item["id"]: item for item in original["items"] if item["id"] not in excluded_ids}
    assert {item["id"] for item in displayed["items"]} == set(expected)
    for field in ("generated_at", "selection_date", "source_observed_at"):
        assert displayed[field] == original[field]
    for item in displayed["items"]:
        source = expected[item["id"]]
        for field in ("source_checked_at", "observed_at", "price", "area", "rooms", "selection_facts", "complex_identity", "price_comparison"):
            assert item[field] == source[field]
        assert len(item["reasons"]) == 3
    assert [item["rank"] for item in displayed["items"]] == list(range(1, len(expected)+1))


def test_daily_enrichment_keeps_checked_facts_and_newer_own_history(monkeypatch):
    original = daily_snapshot(NOW)
    item = original["items"][0]
    item["selection_facts"].update(kitchen_area=12, is_owner=True, owner_source_verified=True)
    item["price_history"] = build_offer_details({"price": item["price"], "price_events": [
        {"old_price": item["price"]+1_000_000, "new_price": item["price"],
         "changed_at": NOW-timedelta(days=1)}]}, "secondary", now=NOW)["price_history"]
    frozen = copy.deepcopy(original)
    market = detailed_market(NOW, item["id"], item["price"], item["area"])
    market["listing_details"][0]["selection_facts"].update(is_owner=False, owner_source_verified=False)
    monkeypatch.setattr("bot.core.daily_picks_display.load_market_snapshot", lambda: market)
    displayed = prepare_daily_picks(original, now=NOW)
    assert original == frozen
    assert_checked_cards_preserved(original, displayed)
    actual = next(card for card in displayed["items"] if card["id"] == item["id"])
    assert any("потолки" in reason for reason in actual["reasons"])
    assert not any("Кухня 15" in reason for reason in actual["reasons"])
    assert not any("2 раза" in reason for reason in actual["reasons"])
    assert actual["price_history"] == item["price_history"]
    assert not any("аналогов:" in reason or "±15" in reason for reason in actual["reasons"])


def test_later_recorded_history_can_enrich_card_without_replacing_checked_facts(monkeypatch):
    original = daily_snapshot(NOW)
    item = original["items"][0]
    item["price_history"] = build_offer_details({"price": item["price"], "price_events": [
        {"old_price": item["price"]+1_000_000, "new_price": item["price"],
         "changed_at": NOW-timedelta(days=10)}]}, "secondary", now=NOW)["price_history"]
    frozen = copy.deepcopy(original)
    market = detailed_market(NOW, item["id"], item["price"], item["area"])
    monkeypatch.setattr("bot.core.daily_picks_display.load_market_snapshot", lambda: market)
    displayed = prepare_daily_picks(original, now=NOW)
    assert original == frozen
    assert_checked_cards_preserved(original, displayed)
    actual = next(card for card in displayed["items"] if card["id"] == item["id"])
    assert actual["price_history"]["changes_count"] == 2
    assert actual["price_history"]["events"][-1]["at"] > item["price_history"]["events"][-1]["at"]


def test_newer_cache_price_mismatch_excludes_only_the_changed_card(monkeypatch):
    original = daily_snapshot(NOW)
    frozen = copy.deepcopy(original)
    item = original["items"][0]
    market = detailed_market(NOW, item["id"], item["price"]+1_000_000, item["area"])
    monkeypatch.setattr("bot.core.daily_picks_display.load_market_snapshot", lambda: market)
    displayed = prepare_daily_picks(original, now=NOW)
    assert original == frozen
    assert item["id"] not in {card["id"] for card in displayed["items"]}
    assert_checked_cards_preserved(original, displayed, excluded_ids={item["id"]})


def test_changed_area_and_large_kitchen_never_break_daily_page_or_promote_old_card(monkeypatch):
    original = daily_snapshot(NOW)
    frozen = copy.deepcopy(original)
    item = original["items"][0]
    market = detailed_market(NOW, item["id"], item["price"], area=100)
    market["listing_details"][0]["selection_facts"]["kitchen_area"] = 50
    monkeypatch.setattr("bot.core.daily_picks_display.load_market_snapshot", lambda: market)
    displayed = prepare_daily_picks(original, now=NOW)
    assert original == frozen
    assert item["id"] not in {card["id"] for card in displayed["items"]}
    assert_checked_cards_preserved(original, displayed, excluded_ids={item["id"]})
    assert all(card["selection_facts"]["kitchen_area"] is None for card in displayed["items"])


@pytest.mark.asyncio
async def test_changed_area_metadata_does_not_break_public_comparison(public_app, monkeypatch):
    market = detailed_market(NOW, area=100)
    market["listing_details"][0]["selection_facts"]["kitchen_area"] = 50
    monkeypatch.setattr("bot.public_comparison_routes.load_market_snapshot", lambda: market)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app), base_url="http://test") as client:
        response = await client.get("/compare?listing=1000000001")
    assert response.status_code == 200
    assert "Кухня 50" not in response.text
    assert "Характеристики объявления изменились" in response.text


def ready_rental_estimate(now):
    return {"monthly_median": 300_000, "monthly_low": 280_000, "monthly_high": 320_000,
            "sample_count": 5, "observed_at": now.isoformat(),
            "oldest_observed_at": (now-timedelta(hours=10)).isoformat(),
            "newest_observed_at": (now-timedelta(hours=1)).isoformat(),
            "basis": "same_complex", "asking_prices": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("estimate", [None, {"status": "ready"},
    {"monthly_low": 280_000, "monthly_high": 320_000},
    {**ready_rental_estimate(NOW-timedelta(hours=73))},
    {**ready_rental_estimate(NOW), "sample_count": 4},
    {**ready_rental_estimate(NOW), "monthly_high": 270_000}])
async def test_unavailable_or_invalid_rental_estimate_has_no_guide_or_placeholder(public_app, monkeypatch, estimate):
    market = detailed_market(NOW)
    market["listing_details"][0]["rental_estimate"] = estimate
    monkeypatch.setattr("bot.public_comparison_routes.load_market_snapshot", lambda: market)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app), base_url="http://test") as client:
        response = await client.get("/compare?listing=1000000001&scope=city")
    assert response.status_code == 200
    assert 'class="mc-rental-guide' not in response.text
    assert "Доход от квартиры" not in response.text
    assert "надёжной оценки аренды" not in response.text
    assert "Выберите «Для сдачи в аренду»" not in response.text
    assert 'data-mc-calculator' in response.text  # Explicit manual scenarios remain usable.


@pytest.mark.asyncio
@pytest.mark.parametrize("banks_ready", [True, False])
async def test_ready_rental_estimate_shows_asking_range_without_unavailable_bank_column(public_app, monkeypatch, banks_ready):
    market = detailed_market(NOW)
    market["listing_details"][0]["rental_estimate"] = ready_rental_estimate(NOW)
    monkeypatch.setattr("bot.public_comparison_routes.load_market_snapshot", lambda: market)
    if not banks_ready:
        monkeypatch.setattr("bot.public_comparison_routes.load_deposit_catalog", lambda: {"offers": []})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app), base_url="http://test") as client:
        response = await client.get("/compare?listing=1000000001")
    assert response.status_code == 200
    assert 'class="mc-rental-guide' in response.text
    assert "280 000 ₸–320 000 ₸" in response.text
    assert "Это цены объявлений" in response.text
    assert ('class="mc-rental-guide-bank"' in response.text) is banks_ready
    assert "Проверяем максимальную ставку" not in response.text
    assert "Свежие подтверждённые условия пока не получены" not in response.text
    if banks_ready:
        assert "19,50%" in response.text and "3 мес." in response.text
        assert "продлении ставка может измениться" in response.text
    if not banks_ready:
        assert "mc-rental-guide-single" in response.text
        assert 'class="mc-bank-section"' not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("bank_count", [0, 3])
async def test_ready_manual_rent_income_survives_missing_banks_without_maximum_claim(public_app, monkeypatch, bank_count):
    catalog = full_deposits(NOW)
    catalog["offers"] = catalog["offers"][:bank_count]
    monkeypatch.setattr("bot.public_comparison_routes.load_deposit_catalog", lambda: catalog)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app), base_url="http://test") as client:
        response = await client.post("/compare/calculate", data={"price": 32_000_000,
            "mode": "rent_out", "monthly_rent": 500_000})
    assert response.status_code == 200
    assert "Доход от аренды после расходов" in response.text
    assert "18,8% за первый год" in response.text
    assert "6 000 000 ₸" in response.text
    assert "Нет свежей подтверждённой ставки" not in response.text
    assert "Для сравнения нужен подтверждённый вклад" not in response.text
    assert "самый доходный подходящий вклад" not in response.text
    assert "Самый доходный подходящий вклад" not in response.text
    assert "В вашем сценарии доходность аренды выше этой ставки." not in response.text
    assert response.text.count('class="mc-bank" ') == bank_count


def test_client_calculation_renderer_only_displays_ready_rental_values_and_banks():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable for isolated client renderer verification")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.hidden = true; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(name, value) { this[name] = value; }
}
const source = fs.readFileSync(process.argv[1], 'utf8');
const flatten = root => [root, ...root.children.flatMap(flatten)];
const text = root => flatten(root).map(node => node.textContent || '').join('\n');
function render(data) {
  const result = new Node('aside');
  const page = {querySelector: query => query === '[data-mc-result]' ? result : null, querySelectorAll: () => []};
  const initial = data ? {textContent: JSON.stringify(data)} : null;
  vm.runInNewContext(source, {Intl, Number, window: {}, document: {
    querySelector: query => query === '.mc-page' ? page : query === '#mc-initial-calculation' ? initial : null,
    createElement: tag => new Node(tag), createDocumentFragment: () => new Node('fragment'),
    createTextNode: text => Object.assign(new Node('text'), {textContent: text})
  }});
  return result;
}
const base = {initial_capital: 32000000, property: {wealth: 38000000}, assumptions: {mode: 'rent_out'}, banks: [],
  rental_benchmark: {net_cash_yield_pct: 18.75, annual_net_cash: 6000000, benchmark: null}};
const noBank = render(base);
assert.equal(noBank.hidden, false);
assert(text(noBank).includes('18,8% за первый год'));
assert(text(noBank).includes('6\u00a0000\u00a0000 ₸'));
assert(!text(noBank).includes('Нет свежей'));
assert(!text(noBank).includes('Для сравнения нужен'));
assert(!text(noBank).includes('—'));
const benchmark = {coverage_complete: true, effective_pct: 19.5, bank: 'Freedom Bank', product: 'Стратегия',
  term_months: 3, source_url: 'https://bankffin.kz/deposits', comparison_note: 'При продлении ставка может измениться.'};
const valid = render({...base, rental_benchmark: {...base.rental_benchmark, benchmark, required_monthly_rent: 520000}});
assert(text(valid).includes('19,5% ГЭСВ'));
assert(text(valid).includes('выше 520\u00a0000 ₸ в месяц'));
const partial = render({...base, rental_benchmark: {...base.rental_benchmark, benchmark: {...benchmark, coverage_complete: false}}});
assert(text(partial).includes('18,8% за первый год'));
assert(!text(partial).includes('19,5% ГЭСВ'));
assert(!text(partial).includes('данные неполны'));
const malformed = render({...base, rental_benchmark: {benchmark}});
assert(!flatten(malformed).some(node => node['data-mc-rental-benchmark'] !== undefined));
const unavailable = render({...base, banks: [{eligible: false, bank: 'UNAVAILABLE', deposit_cash_after_rent: 90000000}]});
assert(!text(unavailable).includes('UNAVAILABLE'));
const blank = render(null);
assert.equal(blank.hidden, true);
assert.equal(blank.children.length, 0);
"""
    result = subprocess.run([node, "-e", script, str(Path(__file__).resolve().parents[1]/"static/hatuli-comparison.js")],
                            capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
