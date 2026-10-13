"""Mock-only regression for Home+'s explicitly restricted VIP column."""
from datetime import datetime, timezone

import pytest

from bot.core import deposit_sources as source
from bot.core.deposit_comparison import best_verified_deposit_benchmark, public_deposit_catalog


NOW = datetime(2026, 10, 13, 13, tzinfo=timezone.utc)


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
