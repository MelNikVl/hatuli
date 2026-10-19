from datetime import datetime, timedelta, timezone
import json

import pytest

from bot.core import deposit_sources as source
from scripts.update_deposit_catalog import build_catalog, discover_programs, write_catalog, fetch_sources
from bot.core.deposit_comparison import load_deposit_catalog, public_deposit_catalog, best_verified_deposit_benchmark

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


def freedom_strategy_html():
    return table([["Валюта", "KZT", "USD"],
                  ["Номинальная ставка", "18.09% 3 мес. 16.40% 6 мес. 13.60% 12 мес. 7.95% 24 мес.", "1.00% 12 мес."],
                  ["Годовая эффективная ставка", "19.50% 3 мес. 17.74% 6 мес. 14.48% 12 мес. 8.23% 24 мес.", "1.00% 12 мес."],
                  ["Минимальная сумма", "15 000 ₸"], ["Пополнение", "есть"], ["Частичное снятие", "нет"]])


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


def fetched_strategy(html=None, document=b"%PDF-baseline"):
    data = {source.FREEDOM_STRATEGY: {"text": html or freedom_strategy_html()}}
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
    offers = source.parse_source(source.HOME_PLUS, home_html(), NOW)
    offer = offers[0]
    assert offer["effective_pct"] == 15.0
    assert offer["nominal_pct"] == 13.98
    assert offer["guarantee_limit"] == 20_000_000
    assert {item["term_months"]: item["effective_pct"] for item in offers} == {3: 19, 6: 18.5, 12: 15}
    assert best_verified_deposit_benchmark({"offers": offers}, NOW)["effective_pct"] == 19
    assert public_deposit_catalog({"offers": offers}, NOW)["banks"][3]["effective_pct"] == 15


def test_freedom_strategy_tracks_195_short_rate_without_rollover_into_annual_product():
    offers = source.parse_source(source.FREEDOM_STRATEGY, freedom_strategy_html(), NOW)
    assert offers[0]["term_months"] == 12
    assert {item["term_months"]: item["effective_pct"] for item in offers} == {3: 19.5, 6: 17.74, 12: 14.48, 24: 8.23}
    assert len({item["id"] for item in offers}) == 4
    assert all(f'{item["term_months"]} месяцев' in item["conditions"][0] for item in offers)
    assert public_deposit_catalog({"offers": offers}, NOW)["banks"][0]["effective_pct"] == 14.48
    benchmark = best_verified_deposit_benchmark({"offers": offers}, NOW)
    assert benchmark["effective_pct"] == 19.5
    assert benchmark["term_months"] == 3
    assert benchmark["short_term"] is True


@pytest.mark.parametrize("changed", [
    freedom_strategy_html().replace("19.50% 3 мес.", "19.50% 6 мес."),
    freedom_strategy_html().replace("18.09% 3 мес.", "18.09% 3 мес. 18.09% 3 мес."),
    freedom_strategy_html().replace("19.50%", "до 19.50%"),
])
def test_freedom_short_rate_rejects_unpaired_duplicate_or_advertised_ambiguity(changed):
    with pytest.raises(ValueError):
        source.parse_source(source.FREEDOM_STRATEGY, changed, NOW)


def test_home_savings_preserves_term_specific_maximum_with_colspan():
    html = table([["Срок депозита", "6 месяцев", "12 месяцев", "24 месяца"],
                  ["Номинальная ставка", "16,55%", "13,98%", "9,53%"],
                  ["Годовая эффективная ставка", "18,00%", "15,00%", "10,00%"],
                  ["Минимальная сумма", "100 000 ₸"], ["Капитализация", "ежедневная"]])
    html = html.replace("</table>", '<tr><td>Максимальная сумма депозита</td><td colspan="2">без ограничений</td><td>100 000 000 ₸</td></tr></table>')
    offers = source.parse_source(source.HOME_SAVINGS, html, NOW)
    assert {item["term_months"]: item["max_amount"] for item in offers} == {6: None, 12: None, 24: 100_000_000}


def test_forte_safe_json_decode_public_display_component_no_javascript_execution():
    offers = source.parse_source(source.FORTE, forte_html(), NOW)
    offer = offers[0]
    assert offer["nominal_pct"] == 14.93
    assert offer["effective_pct"] == 16.0
    assert "32" in offer["early_termination"]
    assert {item["term_months"]: item["effective_pct"] for item in offers} == {3: 19.1, 6: 16.9, 12: 16}
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


def test_changed_pdf_review_persists_for_new_short_programs_across_two_refreshes():
    baseline = build_catalog({}, fetched_strategy(), NOW)
    baseline["offers"] = [item for item in baseline["offers"] if item["term_months"] == 12]
    changed_fetch = fetched_strategy(document=b"%PDF-changed conditions")
    first = build_catalog(baseline, changed_fetch, NOW + timedelta(hours=6))
    # Simulate a new parser recognizing additional terms only on the next run.
    before_expansion = {**first, "offers": [item for item in first["offers"] if item["term_months"] == 12]}
    second = build_catalog(before_expansion, changed_fetch, NOW + timedelta(hours=12))
    for data in (first, second):
        assert len(data["offers"]) == 4
        assert all(item["status"] == "needs_review" for item in data["offers"])
        assert best_verified_deposit_benchmark(data, NOW + timedelta(hours=12)) is None
        document = next(item for item in data["sources"] if item["url"] == "https://bankffin.kz/docs/strategy_condition_ru.pdf")
        assert document["status"] == "needs_review"
        assert document["review_required"] is True
        assert document["checked_at"] == NOW.isoformat()
        assert document["source_hash"] != document["pending_source_hash"]
    assert sum(item["kind"] == "document_changed" for item in first["changes"]) == sum(
        item["kind"] == "document_changed" for item in second["changes"])
    accepted = build_catalog(second, changed_fetch, NOW + timedelta(hours=18), accept_terms=True)
    assert all(item["status"] == "verified" for item in accepted["offers"])
    assert best_verified_deposit_benchmark(accepted, NOW + timedelta(hours=18))["effective_pct"] == 19.5
    document = next(item for item in accepted["sources"] if item["url"] == "https://bankffin.kz/docs/strategy_condition_ru.pdf")
    assert document["status"] == "verified"
    assert document["review_required"] is False
    assert "pending_source_hash" not in document


def test_condition_review_applies_to_new_terms_and_survives_failed_following_refresh():
    baseline = build_catalog({}, fetched_strategy(), NOW)
    baseline["offers"] = [item for item in baseline["offers"] if item["term_months"] == 12]
    # Core eligibility is unchanged, but a fee row now requires review.
    changed_html = freedom_strategy_html().replace("</table>", "<tr><td>Перевод</td><td>Новая комиссия 1%</td></tr></table>")
    first = build_catalog(baseline, fetched_strategy(changed_html), NOW + timedelta(hours=6))
    assert all(item["status"] == "needs_review" for item in first["offers"])
    assert all(item["checked_at"] is None for item in first["offers"] if item["term_months"] != 12)
    failed_fetch = fetched_strategy(changed_html)
    failed_fetch[source.FREEDOM_STRATEGY] = {"error": "HTTP 503"}
    failed = build_catalog(first, failed_fetch, NOW + timedelta(hours=9))
    assert all(item["status"] == "unavailable" for item in failed["offers"])
    before_expansion = {**failed, "offers": [item for item in failed["offers"] if item["term_months"] == 12]}
    second = build_catalog(before_expansion, fetched_strategy(changed_html), NOW + timedelta(hours=12))
    assert all(item["status"] == "needs_review" for item in second["offers"])
    assert best_verified_deposit_benchmark(second, NOW + timedelta(hours=12)) is None
    accepted = build_catalog(second, fetched_strategy(changed_html), NOW + timedelta(hours=18), accept_terms=True)
    assert all(item["status"] == "verified" for item in accepted["offers"])


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
