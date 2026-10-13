"""Public price-history blocks render real charts without unavailable-data copy."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
import pytest

from bot.core.comparison_offer_details import price_history
from bot.core.price_history_chart import price_history_chart


NOW = datetime(2026, 10, 13, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def render_history():
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parents[1] / "bot" / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["price_history_chart"] = price_history_chart
    env.filters["fmt_num"] = lambda number, digits=0: f"{number:,.{digits}f}".replace(",", " ").replace(".", ",")
    return env.get_template("_price_history_chart.html").module.render_price_history


@pytest.mark.parametrize("history", [
    None,
    {},
    {"status": "unavailable", "events": [], "warning": "NOT FOR BUYER"},
    {"status": "no_changes_recorded", "events": []},
    {"status": "incomplete", "events": [], "warning": "NOT FOR BUYER"},
    {"status": "consistent", "events": [
        {"at": NOW.isoformat(), "old_price": 34_000_000, "new_price": 0}]},
    {"status": "consistent", "events": [
        {"at": "undated", "old_price": 34_000_000, "new_price": 35_000_000}]},
])
def test_missing_or_unusable_history_has_no_section_title_or_explanation(render_history, history):
    assert str(render_history(history)).strip() == ""


def test_confirmed_price_changes_keep_chart_dates_and_accessible_table(render_history):
    events = [
        {"at": NOW - timedelta(days=3), "old_price": 34_000_000, "new_price": 35_000_000},
        {"at": NOW - timedelta(days=1), "old_price": 35_000_000, "new_price": 33_000_000},
    ]
    html = str(render_history(price_history(events, 33_000_000, now=NOW)))
    assert 'aria-label="История цены предложения"' in html
    assert "Как менялась цена" in html
    assert "data-price-history-chart" in html
    assert 'data-direction="up"' in html and 'data-direction="down"' in html
    assert "Все изменения по датам" in html
    assert "34 000 000" in html and "35 000 000" in html and "33 000 000" in html
    assert "11.10.2026" in html and "13.10.2026" in html
    assert "История изменений цены пока не загружена" not in html


def test_real_events_with_known_integrity_gap_keep_warning_and_no_total_claim(render_history):
    events = [
        {"at": NOW - timedelta(days=3), "old_price": 34_000_000, "new_price": 35_000_000},
        {"at": NOW - timedelta(days=1), "old_price": 37_000_000, "new_price": 33_000_000},
    ]
    html = str(render_history(price_history(events, 33_000_000, now=NOW)))
    assert "data-price-history-chart" in html
    assert 'class="hatuli-price-history-warning"' in html
    assert 'class="hatuli-price-history-change"' not in html
