"""Forecast presentation must distinguish current evidence from future prices."""
from copy import deepcopy
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


@pytest.fixture
def render():
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).parents[1] / "bot" / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    environment.filters["fmt_num"] = lambda value, precision=0: f"{value:,.{precision}f}".replace(",", " ").replace(".", ",")
    template = environment.get_template("_price_forecast.html")
    return lambda payload: template.render(price_forecast=payload, forecast_compact=True)


def payload():
    return {
        "as_of": "2026-10-13T06:00:00+00:00",
        "market_now": {
            "status": "ready", "estimate": 30_000_000, "low": 28_000_000,
            "high": 32_000_000, "analog_count": 18, "basis": "в том же ЖК",
        },
        "forecasts": [{
            "status": "ready", "horizon_days": 7,
            "estimate": 31_500_000, "low": 29_000_000, "high": 33_000_000,
            "historical_analog_count": 32, "target_date": "2026-10-20",
            "validation": {"sample_size": 125, "mape_pct": 1.3, "baseline_mape_pct": 2.4, "coverage_pct": 82},
        }, {
            "status": "insufficient_history", "horizon_days": 30,
            "estimate": None, "low": None, "high": None,
            "reason": "Нужны последовательные периоды для проверки.",
        }],
        "methodology_label": "Прошлые похожие квартиры и проверка на следующих периодах.",
        "limitations": ["Заявленная цена может отличаться от цены сделки."],
    }


def test_current_market_and_validated_future_have_distinct_labels_and_dates(render):
    html = render(payload())
    assert "Оценка рынка сегодня" in html
    assert "30 000 000 ₸" in html
    assert "Цена предложения через 7 дней" in html
    assert "31 500 000 ₸" in html
    assert "20.10.2026" in html and "13.10.2026" in html
    assert "32" in html and "125 наблюдений" in html
    assert "Средняя процентная ошибка: 1,3%" in html
    assert "простого ориентира «цена не изменится»: 2,4%" in html
    assert "30 дней надёжного прогноза пока нет" in html
    assert "не прогноз доходности за год" in html


@pytest.mark.parametrize("status", ["quality_gate_failed", "insufficient_history", "insufficient_analogs", "stale_model"])
def test_abstention_hides_precomputed_prediction_even_if_numbers_remain(render, status):
    data = payload()
    data["forecasts"][0]["status"] = status
    html = render(data)
    assert "31 500 000" not in html and "29 000 000" not in html
    assert "30 000 000 ₸" in html
    assert 'data-forecast-status="' + status + '"' in html


@pytest.mark.parametrize("change", [
    {"low": 32_000_000}, {"high": 31_000_000}, {"historical_analog_count": 0},
    {"validation": {"sample_size": 0, "mape_pct": 1.3}},
    {"validation": {"sample_size": 125}}, {"estimate": float("nan")},
    {"target_date": None},
])
def test_ready_flag_alone_cannot_display_an_invalid_interval_or_unvalidated_forecast(render, change):
    data = deepcopy(payload())
    data["forecasts"][0].update(change)
    html = render(data)
    assert "31 500 000" not in html
    assert "Средняя процентная ошибка" not in html


def test_untrusted_analog_reason_and_limitations_are_escaped(render):
    data = payload()
    data["market_now"]["basis"] = '<img src=x onerror="alert(1)">'
    data["forecasts"][1]["reason"] = "<script>PRIVATE-ATTACK</script>"
    data["limitations"] = ["<strong>INJECT</strong>"]
    html = render(data)
    assert "<img" not in html and "<script" not in html and "<strong>INJECT</strong>" not in html
    assert "&lt;script&gt;PRIVATE-ATTACK&lt;/script&gt;" in html


def test_empty_projection_is_an_explicit_unknown_not_a_zero_forecast(render):
    html = render({"forecasts": [], "market_now": None})
    assert "Прогноз пока недоступен" in html
    assert "Для прогноза нужны похожие квартиры" in html
    assert 'class="pf-amount"' not in html
