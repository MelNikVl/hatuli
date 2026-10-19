"""Checked bank notice periods remain fact tokens, not invented forecasts."""
from datetime import timedelta

import pytest

from bot.core import analytics_articles as core
from tests.test_analytics_articles import NOW
from tests.test_analytics_articles_comparisons import bank_edition


@pytest.fixture(autouse=True)
def deny_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/hatuli_term_days_test")
    async def denied(*args, **kwargs):
        raise AssertionError("Term facts must not access PostgreSQL")
    monkeypatch.setattr("asyncpg.connect", denied)
    monkeypatch.setattr("asyncpg.create_pool", denied)
    monkeypatch.setattr("bot.db.pg.init_pool", denied)


def notice_edition(days=30):
    value = bank_edition()
    source = value["digest"]["sources"]["bank.freedom"]
    value["digest"]["facts"]["bank.freedom.notice_days"] = {
        "label": "Срок возврата после заявления", "value": days, "unit": "дней", "n": None,
        "as_of": source["as_of"], "source_id": "bank.freedom"}
    value["articles"][0]["key_points"][2] = (
        "Досрочный возврат происходит через {fact:bank.freedom.notice_days} после заявления; "
        "срок доступа к деньгам нужно учитывать вместе со ставкой выбранного вклада.")
    return value


def test_checked_official_notice_days_resolve_in_prose_with_existing_percent_and_money():
    value = notice_edition()
    value["digest"]["facts"]["bank.freedom.minimum_amount"] = {
        "label": "Минимальная сумма вклада", "value": 10000, "unit": "₸", "n": None,
        "as_of": value["digest"]["sources"]["bank.freedom"]["as_of"], "source_id": "bank.freedom"}
    article = core.normalize_edition(value, now=NOW)[0]
    assert "через 30 дней после заявления" in article["key_points"][2]
    assert article["comparisons"][0]["rows"][0]["value_label"] == "16,87%"
    assert core.number_label(10000, "₸") == "10 000 ₸"
    assert all("Опубликовано" not in source["as_of_label"] for source in article["sources"][-2:])


@pytest.mark.parametrize("days", [0, 30, 36600])
def test_whole_days_have_explicit_bounded_domain(days):
    assert core.normalize_edition(notice_edition(days), now=NOW)


@pytest.mark.parametrize("days", [30.5, -1, 36601, 10**12, True, float("inf"), float("nan")])
def test_fractional_negative_giant_or_nonfinite_days_are_rejected(days):
    with pytest.raises(ValueError):
        core.normalize_edition(notice_edition(days), now=NOW)


@pytest.mark.parametrize("change", ["wrong_host", "stale", "wrong_date", "fake_n", "uncleared_rights"])
def test_notice_days_do_not_bypass_bank_host_freshness_rights_or_sample_gates(change):
    value = notice_edition()
    source = value["digest"]["sources"]["bank.freedom"]
    fact = value["digest"]["facts"]["bank.freedom.notice_days"]
    if change == "wrong_host": source["url"] = "https://bankffin.kz.example.org/terms"
    if change == "stale":
        source["as_of"] = (NOW-timedelta(hours=25)).isoformat()
        for entry in value["digest"]["facts"].values():
            if entry["source_id"] == "bank.freedom": entry["as_of"] = source["as_of"]
    if change == "wrong_date": fact["as_of"] = (NOW-timedelta(hours=1)).isoformat()
    if change == "fake_n": fact["n"] = 30
    if change == "uncleared_rights": source["rights_status"] = "not_cleared"
    with pytest.raises(ValueError):
        core.normalize_edition(value, now=NOW)
