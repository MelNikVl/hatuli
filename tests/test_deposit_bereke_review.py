"""Mocked retail tables: product boundaries must not borrow adjacent terms."""
from datetime import datetime, timedelta, timezone

import pytest

from bot.core import deposit_sources as source
from bot.core.deposit_comparison import public_deposit_catalog
from scripts.update_deposit_catalog import build_catalog


NOW = datetime(2026, 10, 14, 15, tzinfo=timezone.utc)
TERM = "Bereke депозит (срочный)"
TOPUP = "Bereke с пополнением (срочный)"
TERM_RATE = ("Номинальная ставка вознаграждения 3 месяца - 16,67% (ГЭСВ*- 18,01%) "
             "6 месяцев - 16,49% (ГЭСВ*- 17,80%) 12 месяцев - 14,67% (ГЭСВ*- 15,70%) "
             "24 месяца -11,57% (ГЭСВ*- 12,20%) * Годовая эффективная ставка вознаграждения")
TOPUP_RATE = ("Номинальная ставка вознаграждения 3 месяца - 17,52% (ГЭСВ*- 19,00%) "
              "6 месяцев - 17,09% (ГЭСВ*- 18,50%) 12 месяцев - 14,06% (ГЭСВ*- 15,00%) "
              "24 месяца - 4,98% (ГЭСВ*- 5,10%) * Годовая эффективная ставка вознаграждения")
ROLLOVER = ("Пролонгация не осуществляется. Сумма вклада размещается в срочный вклад "
            "«Bereke депозит со снятием» на 3 месяца в той же валюте, на условиях и по ставке "
            "вознаграждения, действующих в Банке по данному виду вклада на дату размещения")
CAPITALIZATION = ("Начисление вознаграждения - ежемесячная капитализация и выплата в конце срока "
                  "депозита - ежемесячная выплата на текущий счет")


def conditions(top_up=False):
    return [
        "Срок 3, 6, 12, 24 месяцев",
        "Валюта теңге",
        "Минимальная сумма 5 000 теңге",
        "Максимальная сумма 10 000 000 000 теңге",
        ("Дополнительные взносы осуществляется до максимальной суммы вклада" if top_up
         else "Дополнительные взносы не предусмотрены"),
        "Частичные изъятия не предусмотрены",
        ("Досрочное расторжение предусмотрено. Досрочный возврат осуществляется не позднее "
         "7 календарных дней. Вознаграждение по депозиту пересчитывается по ставке 0%"
         + ("" if top_up else ".")),
        ROLLOVER,
        CAPITALIZATION,
    ]


def block(name, rate, terms=None, span=None):
    terms = conditions(name == TOPUP) if terms is None else terms
    span = len(terms) + 1 if span is None else span
    return (f'<tr><td rowspan="{span}">{name}</td><td>{rate}</td></tr>'
            + "".join(f"<tr><td>{value}</td></tr>" for value in terms))


def page(*blocks):
    return ("<html><title>Bereke: розничные вклады</title><table><tbody>"
            + "".join(blocks) + "</tbody></table>" + " " * 500 + "</html>")


def historical():
    return page(block(TERM, TERM_RATE), block(TOPUP, TOPUP_RATE),
                block("Bereke депозит Табыс", "1 месяц - 7,66% (ГЭСВ*- 7,96%)"))


def current(*, term_terms=None, topup_terms=None):
    return page(block(TERM, TERM_RATE, term_terms), block(TOPUP, TOPUP_RATE, topup_terms),
                block("Bereke депозит с пополнением", "4 месяца – 17,52% (ГЭСВ*-19,00%)"),
                block("Bereke депозит со снятием", "12 месяцев - 14,41% (ГЭСВ - 15,40%)"),
                block("Bereke депозит Табыс", "1 месяц - 7,66% (ГЭСВ*- 7,96%)"))


def fetched(html):
    return {source.BEREKE: {"text": html}}


@pytest.mark.parametrize("html", [historical(), current(), page(
    block("Другой продукт", "12 месяцев - 21,00% (ГЭСВ - 25,00%)"),
    block(TOPUP, TOPUP_RATE), block(TERM, TERM_RATE),
)])
def test_exact_product_rowspans_preserve_rates_and_historical_terms_hashes(html):
    offers = source.parse_source(source.BEREKE, html, NOW)
    assert [(item["id"], item["nominal_pct"], item["effective_pct"]) for item in offers] == [
        ("bereke-term-12", 14.67, 15.7), ("bereke-topup-12", 14.06, 15.0)]
    assert [item["terms_hash"] for item in offers] == [
        "910c24ed9a5aaf5708779e970195b44acf8663226bac0f090c679aff6f7cb40f",
        "c4f34fb780c9873d4ebad6d41122bf3e368122516998c87bc5d18411fcf570d0"]
    for item in offers:
        assert item["term_months"] == 12
        assert item["currency"] == "KZT"
        assert item["deposit_type"] == "other" and item["guarantee_limit"] == 10_000_000
        assert item["min_amount"] == 5000 and item["max_amount"] == 10_000_000_000
        assert item["can_withdraw"] is False and item["capitalization"] == "ежемесячная"
        assert item["warnings"] == ["ГЭСВ предполагает сохранение процентов, а не выплаты на другой счёт."]
    assert [item["can_top_up"] for item in offers] == [False, True]


def test_new_adjacent_products_do_not_require_review_of_unchanged_tracked_products():
    before = build_catalog({}, fetched(historical()), NOW)
    after = build_catalog(before, fetched(current()), NOW + timedelta(hours=6))
    assert all(item["status"] == "verified" for item in after["offers"])
    assert len(after["offers"]) == 2
    assert not after["changes"]
    bank = public_deposit_catalog(after, NOW + timedelta(hours=6))["banks"][2]
    assert bank["eligible"] is True and bank["effective_pct"] == 15.7


def test_true_rate_only_change_is_recorded_without_changing_terms_hash():
    before = build_catalog({}, fetched(current()), NOW)
    changed = current().replace("12 месяцев - 14,67% (ГЭСВ*- 15,70%)",
                                "12 месяцев - 14,90% (ГЭСВ*- 16,00%)")
    after = build_catalog(before, fetched(changed), NOW + timedelta(hours=6))
    term, topup = after["offers"]
    assert (term["nominal_pct"], term["effective_pct"]) == (14.9, 16.0)
    assert term["terms_hash"] == before["offers"][0]["terms_hash"]
    assert topup["terms_hash"] == before["offers"][1]["terms_hash"]
    assert all(item["status"] == "verified" for item in after["offers"])
    assert after["changes"] == [{"kind": "rate_changed", "id": "bereke-term-12",
                                "at": (NOW + timedelta(hours=6)).isoformat(),
                                "old_nominal_pct": 14.67, "old_effective_pct": 15.7,
                                "new_nominal_pct": 14.9, "new_effective_pct": 16.0}]


@pytest.mark.parametrize("span", ["", "zero", "0", "1", "9", "11", "999", "-10", "10.0"])
def test_missing_malformed_short_or_overlapping_product_span_is_rejected(span):
    html = current().replace(f'<td rowspan="10">{TOPUP}', f'<td rowspan="{span}">{TOPUP}', 1)
    with pytest.raises(ValueError):
        source.parse_source(source.BEREKE, html, NOW)


@pytest.mark.parametrize("kind", ["duplicate_product", "duplicate_table", "missing_span",
                                  "extra_cell", "colspan", "rate_rowspan", "nested", "previous_overlap"])
def test_ambiguous_table_structure_is_rejected(kind):
    html = current()
    if kind == "duplicate_product":
        html = html.replace("</tbody>", block(TOPUP, TOPUP_RATE) + "</tbody>")
    elif kind == "duplicate_table":
        html = html.replace("</html>", "<table>" + block(TOPUP, TOPUP_RATE) + "</table></html>")
    elif kind == "missing_span":
        html = html.replace(f'<td rowspan="10">{TOPUP}', f"<td>{TOPUP}")
    elif kind == "extra_cell":
        html = html.replace("<td>Валюта теңге</td>", "<td>Валюта теңге</td><td>USD</td>", 1)
    elif kind == "colspan":
        html = html.replace("<td>Валюта теңге</td>", '<td colspan="2">Валюта теңге</td>', 1)
    elif kind == "rate_rowspan":
        html = html.replace("<td>Номинальная ставка", '<td rowspan="2">Номинальная ставка', 1)
    elif kind == "nested":
        html = html.replace("<td>Валюта теңге</td>", "<td>Валюта теңге<table><tr><td>USD</td></tr></table></td>", 1)
    else:
        # A valid-looking target header remains inside a previous merged cell.
        html = html.replace("<tbody>", '<tbody><tr><td rowspan="2">Other</td><td>Other rates</td></tr>')
    with pytest.raises(ValueError):
        source.parse_source(source.BEREKE, html, NOW)


@pytest.mark.parametrize("name,index,replacement", [
    (TERM, 0, "Срок 3, 6, 24 месяцев"),
    (TERM, 0, "Срок 3, 6, 12, 12 месяцев"),
    (TERM, 1, "Валюта USD"),
    (TOPUP, 1, "Валюта теңге, USD"),
    (TERM, 2, "Минимальная сумма 15 000 теңге"),
    (TOPUP, 3, "Максимальная сумма 20 000 000 000 теңге"),
    (TERM, 4, "Дополнительные взносы осуществляется до максимальной суммы вклада"),
    (TOPUP, 4, "Дополнительные взносы не предусмотрены"),
    (TOPUP, 5, "Частичные изъятия предусмотрены"),
    (TERM, 6, conditions()[6].replace("7 календарных", "10 календарных")),
    (TOPUP, 6, conditions(True)[6].replace("не позднее", "не ранее")),
    (TOPUP, 6, conditions(True)[6].replace("0%", "0,1%")),
    (TERM, 8, CAPITALIZATION.replace("ежемесячная капитализация", "ежедневная капитализация")),
    (TOPUP, 8, "Начисление вознаграждения ежемесячная выплата на текущий счет"),
])
def test_changed_own_conditions_are_not_borrowed_from_neighbouring_products(name, index, replacement):
    terms = conditions(name == TOPUP)
    terms[index] = replacement
    html = current(**{"term_terms" if name == TERM else "topup_terms": terms})
    with pytest.raises(ValueError):
        source.parse_source(source.BEREKE, html, NOW)


@pytest.mark.parametrize("index", range(9))
def test_missing_own_condition_cannot_be_filled_by_neighbour(index):
    terms = conditions(True)
    terms.pop(index)
    with pytest.raises(ValueError):
        source.parse_source(source.BEREKE, current(topup_terms=terms), NOW)


def test_duplicate_own_condition_is_rejected():
    terms = conditions(True)
    terms.append(terms[1])
    with pytest.raises(ValueError):
        source.parse_source(source.BEREKE, current(topup_terms=terms), NOW)


@pytest.mark.parametrize("rate", [
    TOPUP_RATE.replace("12 месяцев -", "13 месяцев -"),
    TOPUP_RATE.replace("12 месяцев - 14,06%", "12 месяцев - до 14,06%"),
    TOPUP_RATE + " 12 месяцев - 14,06% (ГЭСВ*- 15,00%)",
    TOPUP_RATE.replace("12 месяцев - 14,06%", "12 месяцев - 16,00%"),
])
def test_ambiguous_missing_advertised_or_inconsistent_twelve_month_rate_rejected(rate):
    with pytest.raises(ValueError):
        source.parse_source(source.BEREKE, page(block(TERM, TERM_RATE), block(TOPUP, rate)), NOW)


def test_changed_extra_conditions_need_review_and_pending_review_survives_retries():
    before = build_catalog({}, fetched(current()), NOW)
    terms = conditions(True) + ["Канал открытия: только в отделении"]
    changed_html = current(topup_terms=terms)
    pending = build_catalog(before, fetched(changed_html), NOW + timedelta(hours=6))
    assert all(item["status"] == "needs_review" for item in pending["offers"])
    assert all(item["checked_at"] == NOW.isoformat() for item in pending["offers"])
    assert pending["changes"] == [{"kind": "conditions_changed", "id": "bereke-topup-12",
                                   "at": (NOW + timedelta(hours=6)).isoformat()}]
    retry = build_catalog(pending, fetched(changed_html), NOW + timedelta(hours=12))
    assert all(item["status"] == "needs_review" for item in retry["offers"])
    assert retry["changes"] == pending["changes"]
    failed = build_catalog(retry, {source.BEREKE: {"error": "HTTP 503"}}, NOW + timedelta(hours=18))
    recovered = build_catalog(failed, fetched(changed_html), NOW + timedelta(hours=24))
    assert all(item["status"] == "needs_review" for item in recovered["offers"])
    assert not public_deposit_catalog(recovered, NOW + timedelta(hours=24))["banks"][2]["eligible"]


def test_invalid_changed_conditions_disable_old_offers_without_renewing_checked_at():
    before = build_catalog({}, fetched(current()), NOW)
    changed_html = current(topup_terms=[value.replace("Валюта теңге", "Валюта USD")
                                         for value in conditions(True)])
    after = build_catalog(before, fetched(changed_html), NOW + timedelta(hours=6))
    assert all(item["status"] == "unavailable" for item in after["offers"])
    assert all(item["checked_at"] == NOW.isoformat() for item in after["offers"])
    assert [item["effective_pct"] for item in after["offers"]] == [15.7, 15.0]
    assert not public_deposit_catalog(after, NOW + timedelta(hours=6))["banks"][2]["eligible"]
