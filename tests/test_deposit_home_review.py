"""Mock-only regressions for Home currency/term and restricted VIP columns."""
from datetime import datetime, timezone

import pytest

from bot.core import deposit_sources as source
from bot.core.deposit_comparison import best_verified_deposit_benchmark, public_deposit_catalog


NOW = datetime(2026, 10, 13, 13, tzinfo=timezone.utc)


@pytest.mark.parametrize("label", ["Минимальная сумма депозита", "Капитализация вознаграждения"])
def test_missing_home_condition_value_is_not_a_verified_default(label):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(home_plus_html(), "html.parser")
    row = next(row for row in soup.find_all("tr") if row.find("td").get_text() == label)
    for cell in row.find_all("td")[1:]:
        cell.decompose()
    with pytest.raises(ValueError):
        source.parse_source(source.HOME_PLUS, str(soup), NOW)


def home_plus_html(vip_index=3):
    columns = [("3 месяца", "17,40 %", "19,00%"),
               ("6 месяцев", "16,98%", "18,50%"),
               ("12 месяцев", "13,98%", "15,00%")]
    columns.insert(vip_index, ("для VIP клиентов<br>6 месяцев", "17,90%", "19,60%"))
    rows = [("Срок депозита", [column[0] for column in columns]),
            ("Номинальная ставка", [column[1] for column in columns]),
            ("Годовая эффективная ставка вознаграждения", [column[2] for column in columns])]
    html = "<html><title>Сберегательный Хоум+</title><table>"
    for title, values in rows:
        html += "<tr><td>" + title + "</td>" + "".join("<td>" + value + "</td>" for value in values) + "</tr>"
    for title, value in [("Валюта депозита", "теңге"),
                         ("Минимальная сумма депозита", "1 000 ₸"),
                         ("Максимальная сумма депозита", "без ограничений"),
                         ("Капитализация вознаграждения", "ежедневная"),
                         ("Частичные снятие суммы", "недоступно"),
                         ("Дополнительное пополнение", "без ограничений"),
                         ("Досрочное снятие суммы", "доступно: сумма поступит без вознаграждения на текущий счёт или карту через 30 календарных дней с даты закрытия")]:
        html += f'<tr><td>{title}</td><td colspan="4">{value}</td></tr>'
    return html + "</table>" + " " * 500 + "</html>"


@pytest.mark.parametrize("vip_index", [0, 1, 2, 3])
def test_explicit_home_vip_column_is_excluded_without_shifting_ordinary_rates(vip_index):
    offers = source.parse_source(source.HOME_PLUS, home_plus_html(vip_index), NOW)
    assert {item["term_months"]: (item["nominal_pct"], item["effective_pct"])
            for item in offers} == {3: (17.4, 19), 6: (16.98, 18.5), 12: (13.98, 15)}
    assert all("vip" not in item["id"] and item["max_amount"] is None for item in offers)
    assert best_verified_deposit_benchmark({"offers": offers}, NOW)["effective_pct"] == 19
    twelve = public_deposit_catalog({"offers": offers}, NOW)["banks"][3]
    assert twelve["effective_pct"] == 15
    annual = next(item for item in offers if item["term_months"] == 12)
    assert annual["can_top_up"] is True and annual["can_withdraw"] is False
    assert annual["capitalization"] == "ежедневная" and annual["min_amount"] == 1000
    assert annual["deposit_type"] == "savings" and annual["guarantee_limit"] == 20_000_000


@pytest.mark.parametrize("changed", [
    home_plus_html().replace("для VIP клиентов<br>6 месяцев", "6 месяцев"),
    home_plus_html().replace("для VIP клиентов<br>6 месяцев", "для избранных клиентов 6 месяцев"),
    home_plus_html().replace("для VIP клиентов<br>6 месяцев", "для VIP клиентов 12 месяцев"),
    home_plus_html().replace("<td>17,90%</td>", ""),
    home_plus_html().replace("<td>19,60%</td>", "<td>19,60%</td><td>20,00%</td>"),
    home_plus_html().replace('colspan="4">без ограничений', 'colspan="3">без ограничений'),
])
def test_unknown_restricted_columns_or_misaligned_rates_fail_closed(changed):
    with pytest.raises(ValueError):
        source.parse_source(source.HOME_PLUS, changed, NOW)


def test_vip_exception_is_not_generalized_to_other_home_products():
    with pytest.raises(ValueError):
        source.parse_source(source.HOME_SIMPLE, home_plus_html(), NOW)


def test_source_hash_and_restricted_eligibility_remain_part_of_review_evidence():
    first = source.parse_source(source.HOME_PLUS, home_plus_html(), NOW)
    without_vip = home_plus_html().replace("<td>для VIP клиентов<br>6 месяцев</td>", "").replace(
        "<td>17,90%</td>", "").replace("<td>19,60%</td>", "").replace('colspan="4"', 'colspan="3"')
    second = source.parse_source(source.HOME_PLUS, without_vip, NOW)
    assert first[0]["terms_hash"] != second[0]["terms_hash"]
    assert first[0]["source_hash"] != second[0]["source_hash"]


def home_simple_html(order=None):
    columns = [("теңге", "13 месяцев", "15,10%", "16,30%", "1 000 ₸"),
               ("теңге", "12 месяцев", "13,98%", "15,00%", "1 000 ₸"),
               ("доллар", "12 месяцев", "0,99%", "0,99%", "10 $"),
               ("евро", "12 месяцев", "0,99%", "0,99%", "10 €")]
    if order is not None:
        columns = [columns[index] for index in order]
    html = "<html><title>Депозит Простой</title><table>"
    for title, index in [("Валюта депозита", 0), ("Номинальная ставка", 2),
                         ("Годовая эффективная ставка вознаграждения", 3),
                         ("Минимальная сумма депозита", 4)]:
        html += "<tr><td>" + title + "</td>" + "".join("<td>" + column[index] + "</td>" for column in columns) + "</tr>"
    if order is None:
        html += '<tr><td>Срок депозита</td><td>13 месяцев</td><td colspan="3">12 месяцев</td></tr>'
    else:
        html += "<tr><td>Срок депозита</td>" + "".join("<td>" + column[1] + "</td>" for column in columns) + "</tr>"
    for title, value in [("Максимальная сумма депозита", "без ограничений"),
                         ("Капитализация вознаграждения", "ежедневная"),
                         ("Частичные снятие суммы", "доступно"),
                         ("Дополнительное пополнение", "доступно"),
                         ("Досрочное изъятие денег", "сумма с вознаграждением поступит в течение 24 часов на карту или текущий счёт")]:
        html += f'<tr><td>{title}</td><td colspan="4">{value}</td></tr>'
    return html + "</table>" + " " * 500 + "</html>"


@pytest.mark.parametrize("order", [None, [2, 0, 3, 1], [3, 1, 2, 0]])
def test_home_simple_explicit_colspan_pairs_kzt_rates_without_foreign_currency_leakage(order):
    offers = source.parse_source(source.HOME_SIMPLE, home_simple_html(order), NOW)
    assert {item["term_months"]: (item["nominal_pct"], item["effective_pct"])
            for item in offers} == {12: (13.98, 15), 13: (15.1, 16.3)}
    assert {item["id"] for item in offers} == {"home-simple-12", "home-simple-13"}
    assert all(item["currency"] == "KZT" and item["min_amount"] == 1000 for item in offers)
    assert all(item["can_top_up"] and item["can_withdraw"] for item in offers)
    assert all(item["max_amount"] is None and item["capitalization"] == "ежедневная" for item in offers)
    assert all(item["deposit_type"] == "other" and item["guarantee_limit"] == 10_000_000 for item in offers)
    assert public_deposit_catalog({"offers": offers}, NOW)["banks"][3]["effective_pct"] == 15


@pytest.mark.parametrize("changed", [
    home_simple_html().replace('colspan="3">12 месяцев', 'colspan="2">12 месяцев'),
    home_simple_html().replace('colspan="3">12 месяцев', 'colspan="1000000">12 месяцев'),
    home_simple_html().replace('colspan="3">12 месяцев', 'rowspan="2" colspan="3">12 месяцев'),
    home_simple_html().replace("<td>евро</td>", "<td>валюта не указана</td>"),
    home_simple_html().replace("<td>13 месяцев</td>", "<td>12 месяцев</td>"),
    home_simple_html().replace("<td>15,10%</td>", ""),
    home_simple_html().replace("<td>16,30%</td>", "<td>16,30%</td><td>17,00%</td>"),
    home_simple_html().replace('colspan="4">ежедневная', 'colspan="3">ежедневная'),
    home_simple_html().replace("<td>1 000 ₸</td><td>1 000 ₸</td>", "<td>1 000 ₸</td><td>10 $</td>"),
])
def test_home_simple_unknown_currency_and_inconsistent_grid_fail_closed(changed):
    with pytest.raises(ValueError):
        source.parse_source(source.HOME_SIMPLE, changed, NOW)
