"""Only validated future price forecasts produce a public forecast section."""
from copy import deepcopy
from datetime import datetime, timezone
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

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
    assert "30 дней" not in html
    assert "Нужны последовательные периоды для проверки." not in html
    assert "не прогноз доходности за год" in html


@pytest.mark.parametrize("status", ["quality_gate_failed", "insufficient_history", "insufficient_analogs", "stale_model", "missing_source_history", "missing_attribute_history", "insufficient_groups", "insufficient_listing_history"])
def test_abstention_hides_precomputed_prediction_even_if_numbers_remain(render, status):
    data = payload()
    data["forecasts"][0]["status"] = status
    html = render(data)
    assert html.strip() == ""


@pytest.mark.parametrize("change", [
    {"low": 32_000_000}, {"high": 31_000_000}, {"historical_analog_count": 0},
    {"validation": {"sample_size": 0, "mape_pct": 1.3}},
    {"validation": {"sample_size": 125}}, {"estimate": float("nan")},
    {"estimate": None}, {"low": None}, {"high": None},
    {"horizon_days": 0}, {"horizon_days": None},
    {"target_date": None}, {"as_of": None},
])
def test_ready_flag_alone_cannot_display_an_invalid_interval_or_unvalidated_forecast(render, change):
    data = deepcopy(payload())
    data["forecasts"][0].update(change)
    if "as_of" in change:
        data["as_of"] = None
    assert render(data).strip() == ""


@pytest.mark.parametrize("field", ["estimate", "low", "high", "horizon_days", "historical_analog_count", "sample_size", "mape_pct"])
@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), float("-inf")])
def test_nonfinite_or_boolean_forecast_numbers_do_not_create_a_section(render, field, value):
    data = payload()
    if field in {"sample_size", "mape_pct"}:
        data["forecasts"][0]["validation"][field] = value
    else:
        data["forecasts"][0][field] = value
    assert render(data).strip() == ""


def test_mixed_horizons_render_only_the_available_forecast(render):
    data = payload()
    invalid = deepcopy(data["forecasts"][0])
    invalid.update(horizon_days=90, historical_analog_count=0, reason="INVALID-HORIZON")
    data["forecasts"].append(invalid)
    html = render(data)
    assert html.count('class="hatuli-forecast') == 1
    assert "Цена предложения через 7 дней" in html
    assert "Горизонт 7 дней" in html
    assert "30 дней" not in html and "90 дней" not in html
    assert "Нужны последовательные периоды" not in html
    assert "INVALID-HORIZON" not in html
    assert "pf-unavailable" not in html and "pf-horizons" not in html


def test_unavailable_current_estimate_does_not_distract_from_valid_future(render):
    data = payload()
    data["market_now"] = {"status": "insufficient_analogs", "reason": "NO-CURRENT-ESTIMATE"}
    html = render(data)
    assert "Цена предложения через 7 дней" in html
    assert "Оценка рынка сегодня" not in html
    assert "NO-CURRENT-ESTIMATE" not in html


@pytest.mark.parametrize("field", ["estimate", "low", "high", "analog_count"])
@pytest.mark.parametrize("value", [True, float("nan"), float("inf")])
def test_malformed_current_estimate_is_hidden_but_valid_future_remains(render, field, value):
    data = payload()
    data["market_now"][field] = value
    html = render(data)
    assert "Цена предложения через 7 дней" in html
    assert "Оценка рынка сегодня" not in html


def test_empty_limitations_never_create_blank_bullets(render):
    data = payload()
    data["limitations"] = ["", "   ", None, 0, False]
    html = render(data)
    assert "Цена предложения через 7 дней" in html
    assert "<ul>" not in html and "<li>" not in html
    data["limitations"].append("Заявленная цена может отличаться от цены сделки.")
    html = render(data)
    assert html.count("<li>") == 1
    assert "<li>Заявленная цена может отличаться от цены сделки.</li>" in html


def test_untrusted_analog_reason_and_limitations_are_escaped(render):
    data = payload()
    data["market_now"]["basis"] = '<img src=x onerror="alert(1)">'
    data["forecasts"][1]["reason"] = "<script>PRIVATE-ATTACK</script>"
    data["limitations"] = ["<strong>INJECT</strong>"]
    html = render(data)
    assert "<img" not in html and "<script" not in html and "<strong>INJECT</strong>" not in html
    assert "PRIVATE-ATTACK" not in html
    assert "&lt;img" in html and "&lt;strong&gt;INJECT&lt;/strong&gt;" in html


@pytest.mark.parametrize("data", [None, {}, {"forecasts": [], "market_now": None}, {
    "as_of": "2026-10-13T06:00:00+00:00",
    "market_now": {"status": "ready", "estimate": 30_000_000,
                   "low": 28_000_000, "high": 32_000_000, "analog_count": 18},
    "forecasts": [],
    "methodology_label": "METHOD-WITHOUT-FORECAST",
    "limitations": ["LIMIT-WITHOUT-FORECAST"],
}])
def test_missing_or_market_only_projection_produces_no_section(render, data):
    assert render(data).strip() == ""


def test_actual_model_contract_without_verified_history_is_hidden(render):
    from bot.core.price_forecast import forecast_listing
    data = forecast_listing({}, None, now=datetime(2026, 10, 13, 6, tzinfo=timezone.utc))
    html = render(data)
    assert html.strip() == ""


def test_full_dashboard_javascript_compiles_after_forecast_integration(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable for a JavaScript syntax check")
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).parents[1] / "bot" / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    html = environment.get_template("dashboard.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/map")),
        is_admin=lambda request: False, site_tier="public", listing_id=None,
    )
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.S)
    javascript = "\n".join(script for script in scripts if not script.lstrip().startswith('{"@context"'))
    source = tmp_path / "dashboard.js"
    source.write_text(javascript, encoding="utf-8")
    result = subprocess.run([node, "--check", str(source)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
