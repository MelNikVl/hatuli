from datetime import datetime, timedelta, timezone
import json

import pytest

from bot.core import deposit_sources as source
from scripts.update_deposit_catalog import build_catalog, discover_programs, write_catalog, fetch_sources
from bot.core.deposit_comparison import load_deposit_catalog, public_deposit_catalog

NOW = datetime(2026, 10, 13, 8, tzinfo=timezone.utc)


def table(rows):
    return "<html><title>Банк: депозит</title><table>" + "".join(
        "<tr>" + "".join("<td>" + cell + "</td>" for cell in row) + "</tr>" for row in rows) + "</table>" + " " * 500 + "</html>"


def freedom_html(nominal="14.90", effective="15.95", term="12", minimum="500 ₸"):
    return table([["Валюта", "KZT", "USD"],
                  ["Номинальная ставка", f"{nominal}% {term} мес. 15.23% 13 мес. 14.80% {term} мес. 15.15% 13 мес.", "1.00% 12 мес."],
                  ["Годовая эффективная ставка", f"{effective}% {term} мес. 16.31% 13 мес. 15.95% {term} мес. 16.35% 13 мес.", "1.00% 12 мес."],
                  ["Минимальная сумма", minimum], ["Пополнение", "есть"], ["Частичное снятие", "есть"],
                  ["Комиссия за снятие", "нет при хранении не менее 15 дней; в остальных случаях до 2%"]])


def home_html():
    return table([["Срок депозита", "3 месяца", "6 месяцев", "12 месяцев"],
                  ["Номинальная ставка", "17,40%", "16,98%", "13,98%"],
                  ["Годовая эффективная ставка вознаграждения", "19,00%", "18,50%", "15,00%"],
                  ["Минимальная сумма депозита", "1 000 ₸"], ["Капитализация вознаграждения", "ежедневная"]])


def forte_html():
    values = {"Валюта": [" ", "KZT"], "Срок ": ["3 месяца", "6 месяцев", "12 месяцев"],
              "Номинальная ставка": ["17,60%", "15,72%", "14,93%"],
              "Эффективная ставка (ГЭСВ)": ["19,1%", "16,9%", "16,0%"],
              "Минимальная сумма вклада": [" ", "100 000 ₸"],
              "Досрочное закрытие": [" ", "0,1% годовых, не ранее 32 календарных дней"],
              "Пополнение": [" ", "не предусмотрено"], "Частичное снятие": [" ", "не предусмотрено"]}
    component = {"__typename": "ComponentDynamicComponentsTableCompare", "rows": [
        {"title": title, "values": [{"columnIndex": i, "value": text} for i, text in enumerate(cells)]}
        for title, cells in values.items()]}
    argument = json.dumps([1, "8:" + json.dumps(["$", "Component", None, {"dynamicZones": [component]}])], ensure_ascii=False)
    return "<html><title>Forte</title><script>self.__next_f.push(" + argument + ")</script>" + " " * 500 + "</html>"


def fetched_freedom(html=None, document=b"%PDF-baseline"):
    data = {source.FREEDOM: {"text": html or freedom_html()}}
    data.update({url: {"content": document} for url in source.DOCUMENT_SOURCES})
    return data


def test_freedom_pairs_the_same_currency_term_and_capitalization():
    offers = source.parse_source(source.FREEDOM, freedom_html(), NOW)
    twelve = [offer for offer in offers if offer["term_months"] == 12]
    assert [(offer["nominal_pct"], offer["effective_pct"], offer["capitalization"]) for offer in twelve] == [
        (14.9, 15.95, "ежемесячная"), (14.8, 15.95, "ежедневная")]
    assert len(offers) == 4
    # The 13-month advertised maximum may not enter a 12-month calculation.
    result = public_deposit_catalog({"offers": offers, "checked_at": NOW.isoformat()}, NOW)
    assert result["banks"][0]["effective_pct"] == 15.95


def test_higher_short_term_advertised_home_rate_not_used_for_twelve_months():
    offer = source.parse_source(source.HOME_PLUS, home_html(), NOW)[0]
    assert offer["effective_pct"] == 15.0
    assert offer["nominal_pct"] == 13.98
    assert offer["guarantee_limit"] == 20_000_000


def test_forte_safe_json_decode_public_display_component_no_javascript_execution():
    offer = source.parse_source(source.FORTE, forte_html(), NOW)[0]
    assert offer["nominal_pct"] == 14.93
    assert offer["effective_pct"] == 16.0
    assert "32" in offer["early_termination"]
    with pytest.raises(ValueError):
        source.parse_source(source.FORTE, forte_html().replace("self.__next_f.push", "evil"), NOW)


@pytest.mark.parametrize("html", ["Access denied", freedom_html(term="11"), freedom_html(minimum="5 000 ₸"),
                                  freedom_html(effective="99"), freedom_html(nominal="20", effective="15")])
def test_uncertain_source_rejected_instead_of_falling_back_to_header_advertisement(html):
    with pytest.raises(ValueError):
        source.parse_source(source.FREEDOM, html, NOW)


def test_rate_change_accepted_records_diff_without_terms_change():
    old = build_catalog({}, fetched_freedom(), NOW)
    updated = build_catalog(old, fetched_freedom(freedom_html(nominal="15.10", effective="16.30")), NOW + timedelta(hours=6))
    offer = updated["offers"][0]
    assert offer["effective_pct"] == 16.3
    assert offer["status"] == "verified"
    assert updated["changes"][-1]["kind"] == "rate_changed"


def test_changed_fee_conditions_require_review_and_do_not_self_approve_next_run():
    old = build_catalog({}, fetched_freedom(), NOW)
    changed = freedom_html().replace("до 2%", "до 3%")
    updated = build_catalog(old, fetched_freedom(changed), NOW + timedelta(hours=6))
    assert all(offer["status"] == "needs_review" for offer in updated["offers"])
    assert updated["offers"][0]["checked_at"] == NOW.isoformat()
    again = build_catalog(updated, fetched_freedom(changed), NOW + timedelta(hours=12))
    assert all(offer["status"] == "needs_review" for offer in again["offers"])
    accepted = build_catalog(again, fetched_freedom(changed), NOW + timedelta(hours=18), accept_terms=True)
    assert all(offer["status"] == "verified" for offer in accepted["offers"])


def test_document_change_blocks_calculation_and_keeps_last_confirmed_rates():
    old = build_catalog({}, fetched_freedom(), NOW)
    updated = build_catalog(old, fetched_freedom(document=b"%PDF-new terms"), NOW + timedelta(hours=6))
    assert updated["offers"][0]["effective_pct"] == old["offers"][0]["effective_pct"]
    assert updated["offers"][0]["status"] == "needs_review"
    assert not public_deposit_catalog(updated, NOW + timedelta(hours=6))["banks"][0]["eligible"]


def test_http_failure_retains_prior_value_without_renewing_verified_timestamp():
    old = build_catalog({}, fetched_freedom(), NOW)
    fetched = fetched_freedom()
    fetched[source.FREEDOM] = {"error": "HTTP 403"}
    updated = build_catalog(old, fetched, NOW + timedelta(hours=6))
    assert updated["offers"][0]["effective_pct"] == 15.95
    assert updated["offers"][0]["checked_at"] == NOW.isoformat()
    assert updated["offers"][0]["status"] == "unavailable"
    assert not public_deposit_catalog(updated, NOW + timedelta(hours=6))["banks"][0]["eligible"]


def test_program_discovery_ignores_foreign_business_and_unsafe_urls():
    html = '<a href="/deposits/new">Новое</a><a href="https://evil.test/deposits/no">Bad</a><a href="javascript:alert(1)">bad</a>'
    assert discover_programs("https://home.kz/deposits", html) == ["https://home.kz/deposits/new"]


def test_forte_discovery_sees_new_unknown_program_route_in_deposit_card():
    html = '<div><h3>Новый депозит</h3><a href="/ru/new-program">Подробнее</a></div>'
    assert discover_programs("https://bank.forte.kz/ru/deposits", html) == ["https://bank.forte.kz/ru/new-program"]


def test_atomic_runtime_file_can_be_loaded_and_previous_remains_on_serialization_error(tmp_path):
    path = tmp_path / "catalog.json"
    data = build_catalog({}, fetched_freedom(), NOW)
    write_catalog(path, data)
    assert load_deposit_catalog(path)["offers"][0]["effective_pct"] == 15.95
    before = path.read_bytes()
    with pytest.raises(ValueError):
        write_catalog(path, {"bad": float("nan")})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".deposit-catalog-*.json"))


@pytest.mark.asyncio
async def test_fetch_denied_does_not_follow_redirect_or_use_fallback_user_agent():
    import httpx
    seen = []
    async def handler(request):
        seen.append(request)
        return httpx.Response(468, request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        fetched = await fetch_sources(client)
    assert len(seen) == len(source.PRODUCT_SOURCES) + len(source.INDEX_SOURCES) + len(source.DOCUMENT_SOURCES)
    assert all(item["error"] == "HTTP 468" for item in fetched.values())
