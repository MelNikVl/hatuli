"""Offline forecast publication is guarded and cannot turn bad files into prices."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from bot.core.price_forecast import HORIZONS, METHOD_VERSION
from bot.core.price_forecast_runtime import _read, forecast_for_offer
from scripts.publish_price_forecasts import publish

NOW = datetime(2026, 10, 13, 6, tzinfo=timezone.utc)
PRICE = 30_000_000
IDENTIFIER = "12345"


@pytest.fixture(autouse=True)
def no_postgres_and_clean_cache(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("Offline runtime tests must never connect to PostgreSQL")
    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    _read.cache_clear()
    yield
    _read.cache_clear()


def valid_file():
    metrics = {
        "sample_size": 300, "independent_properties": 250, "changed_properties": 60,
        "rms_log_error": .01, "baseline_rms_log_error": .02,
        "rms_log_improvement_pct": 50, "mape_pct": 1, "baseline_mape_pct": 2,
        "coverage_pct": 82, "mse_improvement_ci95": [.0001, .0004],
        "interval_nominal_coverage_pct": 80, "median_interval_width_pct": 12,
    }
    candidate = {
        "status": "ready", "horizon_days": 7, "estimate": 29_500_000,
        "low": 28_000_000, "high": 31_000_000, "historical_analog_count": 25,
        "target_date": (NOW.date() + timedelta(days=7)).isoformat(),
        "reason": "На исторических похожих квартирах.", "model_version": "unit-test-model",
    }
    return {
        "schema_version": 1, "method_version": METHOD_VERSION,
        "generated_at": NOW.isoformat(), "source_history_verified": True,
        "reports": [{"horizon_days": 7, "status": "ready", "validation": metrics, "fold_count": 2}],
        "offers": {IDENTIFIER: {"price": PRICE, "forecast": {"as_of": NOW.isoformat(), "forecasts": [candidate]}}},
    }


def write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def first(result):
    return result["forecasts"][0]


def assert_no_numbers(result):
    assert all(f["estimate"] is None and f["low"] is None and f["high"] is None for f in result["forecasts"])


def test_validated_fresh_same_price_projection_can_be_shown(tmp_path):
    result = forecast_for_offer(IDENTIFIER, PRICE, now=NOW, path=write(tmp_path / "public.json", valid_file()))
    assert first(result)["status"] == "ready"
    assert first(result)["estimate"] == 29_500_000
    assert first(result)["validation"]["sample_size"] == 300
    assert all(f["estimate"] is None for f in result["forecasts"][1:])


def test_current_price_change_and_unknown_offer_cannot_reuse_projection(tmp_path):
    path = write(tmp_path / "public.json", valid_file())
    assert_no_numbers(forecast_for_offer(IDENTIFIER, PRICE + 1, now=NOW, path=path))
    assert_no_numbers(forecast_for_offer("NOT-PUBLISHED", PRICE, now=NOW, path=path))


def test_inactive_offer_always_abstains_even_with_matching_valid_projection(tmp_path):
    result = forecast_for_offer(IDENTIFIER, PRICE, now=NOW, path=write(tmp_path / "public.json", valid_file()), active=False)
    assert_no_numbers(result)
    assert all(f["status"] == "inactive_listing" for f in result["forecasts"])


@pytest.mark.parametrize("age", [timedelta(hours=49), -timedelta(minutes=1)])
def test_stale_or_future_publication_never_supplies_numbers(tmp_path, age):
    result = forecast_for_offer(IDENTIFIER, PRICE, now=NOW + age, path=write(tmp_path / "public.json", valid_file()))
    assert_no_numbers(result)
    assert all(f["status"] == "stale_model" for f in result["forecasts"])


@pytest.mark.parametrize("change", [
    {"source_history_verified": False},
    {"schema_version": 999}, {"method_version": "unknown"},
    {"generated_at": "not-a-date"}, {"generated_at": "2026-10-13T06:00:00"},
])
def test_invalid_or_unverified_publication_abstains(tmp_path, change):
    data = valid_file()
    data.update(change)
    assert_no_numbers(forecast_for_offer(IDENTIFIER, PRICE, now=NOW, path=write(tmp_path / "public.json", data)))


@pytest.mark.parametrize("metric,value", [
    ("independent_properties", 199), ("changed_properties", 49),
    ("rms_log_improvement_pct", 4.9), ("coverage_pct", 74),
    ("mse_improvement_ci95", [-.0001, .0001]),
    ("mape_pct", float("nan")), ("coverage_pct", float("inf")),
    ("median_interval_width_pct", 41),
    ("mape_pct", -1), ("coverage_pct", 150), ("sample_size", 0),
])
def test_quality_gate_and_nonfinite_metrics_are_enforced(tmp_path, metric, value):
    data = valid_file()
    data["reports"][0]["validation"][metric] = value
    result = forecast_for_offer(IDENTIFIER, PRICE, now=NOW, path=write(tmp_path / "public.json", data))
    assert_no_numbers(result)
    assert first(result)["status"] == "quality_gate_failed"


@pytest.mark.parametrize("field,value", [
    ("estimate", True), ("low", -1), ("low", 30_000_000),
    ("high", float("inf")), ("estimate", "29500000"),
    ("historical_analog_count", 19), ("target_date", "not-a-date"),
])
def test_malformed_candidate_money_and_interval_cannot_be_published_as_fact(tmp_path, field, value):
    data = valid_file()
    data["offers"][IDENTIFIER]["forecast"]["forecasts"][0][field] = value
    assert_no_numbers(forecast_for_offer(IDENTIFIER, PRICE, now=NOW, path=write(tmp_path / "public.json", data)))


@pytest.mark.parametrize("section,value", [
    ("stored", None), ("stored", [1]), ("projection", [1]),
    ("reports", [{"horizon_days": [7], "status": "ready"}]),
    ("candidate", {"horizon_days": [7], "status": "ready"}),
    ("validation", [1]),
    ("report_status", {}), ("candidate_status", {}),
    ("projection_as_of", 1), ("candidate_as_of", 1),
])
def test_malformed_nested_shapes_do_not_break_apartment_pages(tmp_path, section, value):
    data = valid_file()
    if section == "stored":
        data["offers"][IDENTIFIER] = value
    elif section == "projection":
        data["offers"][IDENTIFIER]["forecast"] = value
    elif section == "reports":
        data["reports"] = value
    elif section == "candidate":
        data["offers"][IDENTIFIER]["forecast"]["forecasts"] = [value]
    elif section == "validation":
        data["reports"][0]["validation"] = value
    elif section == "report_status":
        data["reports"][0]["status"] = value
    elif section == "candidate_status":
        data["offers"][IDENTIFIER]["forecast"]["forecasts"][0]["status"] = value
    elif section == "projection_as_of":
        data["offers"][IDENTIFIER]["forecast"]["as_of"] = value
    else:
        data["offers"][IDENTIFIER]["forecast"]["forecasts"][0]["as_of"] = value
    assert_no_numbers(forecast_for_offer(IDENTIFIER, PRICE, now=NOW, path=write(tmp_path / "public.json", data)))


def test_target_date_must_match_advertised_horizon(tmp_path):
    data = valid_file()
    data["offers"][IDENTIFIER]["forecast"]["forecasts"][0]["target_date"] = "2030-01-01"
    assert_no_numbers(forecast_for_offer(IDENTIFIER, PRICE, now=NOW, path=write(tmp_path / "public.json", data)))


def test_old_projection_cannot_be_refreshed_by_copying_a_new_generated_date(tmp_path):
    data = valid_file()
    data["offers"][IDENTIFIER]["forecast"]["as_of"] = (NOW - timedelta(days=10)).isoformat()
    assert_no_numbers(forecast_for_offer(IDENTIFIER, PRICE, now=NOW, path=write(tmp_path / "public.json", data)))


def test_runtime_projection_cannot_leak_training_rows_or_identity_graph(tmp_path):
    data = valid_file()
    data["private_training_rows"] = [{"group_id": "PRIVATE-GROUP"}]
    data["offers"][IDENTIFIER]["forecast"]["private_identity_graph"] = "PRIVATE-GROUP"
    candidate = data["offers"][IDENTIFIER]["forecast"]["forecasts"][0]
    candidate["training_ids"] = ["PRIVATE-TRAINING-ID"]
    candidate["validation"] = {"private_group_id": "PRIVATE-GROUP"}
    result = forecast_for_offer(IDENTIFIER, PRICE, now=NOW, path=write(tmp_path / "public.json", data))
    assert first(result)["status"] == "ready"
    encoded = json.dumps(result)
    assert "PRIVATE-GROUP" not in encoded and "PRIVATE-TRAINING-ID" not in encoded
    assert "training_ids" not in encoded and "private_identity_graph" not in encoded


@pytest.mark.parametrize("field", ["reason", "model_version"])
def test_nested_objects_in_public_text_fields_cannot_leak_training_data(tmp_path, field):
    data = valid_file()
    data["offers"][IDENTIFIER]["forecast"]["forecasts"][0][field] = {"training_ids": ["PRIVATE-TRAINING-ID"]}
    result = forecast_for_offer(IDENTIFIER, PRICE, now=NOW, path=write(tmp_path / "public.json", data))
    assert "PRIVATE-TRAINING-ID" not in json.dumps(result)


def test_missing_or_invalid_file_is_an_explicit_abstention(tmp_path):
    assert_no_numbers(forecast_for_offer(IDENTIFIER, PRICE, now=NOW, path=tmp_path / "missing.json"))
    path = tmp_path / "bad.json"
    path.write_text("not-json", encoding="utf-8")
    assert_no_numbers(forecast_for_offer(IDENTIFIER, PRICE, now=NOW, path=path))


def test_first_day_publication_cannot_invent_forecast_prices(tmp_path):
    observations = tmp_path / "observations"
    observations.mkdir()
    report = publish(observations, tmp_path / "model.json", tmp_path / "public.json", now=NOW)
    public = json.loads((tmp_path / "public.json").read_text(encoding="utf-8"))
    assert report["published_offers"] == 0
    assert public["offers"] == {}
    assert all(h["status"] != "ready" for h in public["reports"])
    assert {h["horizon_days"] for h in public["reports"]} == set(HORIZONS)
    assert "model" not in public and "training_rows" not in public
    assert_no_numbers(forecast_for_offer(IDENTIFIER, PRICE, now=NOW, path=tmp_path / "public.json"))


def test_collector_layout_is_consumed_but_one_real_capture_is_not_enough_for_prediction(tmp_path):
    from bot.core.price_forecast_observations import publish_capture
    observations = tmp_path / "observations"
    row = {
        "listing_id": IDENTIFIER, "entity_kind": "listing", "source": "krisha",
        "market_type": "secondary", "source_status": "active", "source_price_confirmed": True,
        "source_provenance": "sale_search_ingestion", "source_observed_at": NOW.isoformat(),
        "price": PRICE, "rooms": 2, "area": 60,
    }
    capture = publish_capture([row], [{"listing_id": IDENTIFIER}], [], observations, captured_at=NOW)
    assert capture["new_observations"] == 1
    report = publish(observations, tmp_path / "model.json", tmp_path / "public.json", now=NOW)
    assert report["audit"]["input_listings"] == 1
    assert report["published_offers"] == 0
    assert_no_numbers(forecast_for_offer(IDENTIFIER, PRICE, now=NOW, path=tmp_path / "public.json"))


def comparison_setup(monkeypatch, *, primary=False, stale_market=False, stale_picks=False):
    from bot import public_comparison_routes as routes
    observed = "2020-01-01T06:00:00+00:00" if stale_picks else datetime.now(timezone.utc).isoformat()
    offer = {"id": "newbuild:1" if primary else IDENTIFIER, "market_type": "primary" if primary else "secondary",
             "price": PRICE, "rooms": 2, "area": 60, "hex_id": "0_0", "observed_at": observed,
             "build_status": "commissioned", "source_url": "https://krisha.kz/a/show/12345"}
    monkeypatch.setattr(routes, "load_market_snapshot", lambda: {"offers": [offer] if primary else [], "generated_at": observed})
    picks = {"selection_date": observed[:10], "generated_at": observed, "source_observed_at": observed, "items": [] if primary else [offer]}
    monkeypatch.setattr(routes, "load_daily_picks", lambda: picks)
    monkeypatch.setattr(routes, "public_primary_offer", lambda snapshot, identifier: offer if primary else None)
    monkeypatch.setattr(routes, "compare_market", lambda *args, **kwargs: {
        "apartments": [offer] if primary else [], "scope": "city", "rooms": 2,
        "area_min": 50, "area_max": 70, "selected_hex": "0_0", "stale": stale_market,
    })
    monkeypatch.setattr(routes, "load_deposit_catalog", lambda: {})
    monkeypatch.setattr(routes, "public_deposit_catalog", lambda catalog: {})
    monkeypatch.setattr(routes, "compare_investment", lambda *args, **kwargs: {})
    calls = []
    def forecast(identifier, price, **kwargs):
        calls.append(kwargs)
        return {"forecasts": []}
    monkeypatch.setattr(routes, "forecast_for_offer", forecast)
    return routes, calls


def test_stale_daily_pick_in_comparison_cannot_be_presented_as_active_forecast(monkeypatch):
    routes, calls = comparison_setup(monkeypatch, stale_picks=True)
    routes.comparison_context(listing=IDENTIFIER, scope="city")
    assert calls and calls[0].get("active", True) is False


def test_stale_primary_snapshot_in_comparison_cannot_be_presented_as_active_forecast(monkeypatch):
    routes, calls = comparison_setup(monkeypatch, primary=True, stale_market=True)
    routes.comparison_context(unit=1, scope="city")
    assert calls and calls[0].get("active", True) is False


@pytest.mark.parametrize("selector", [{"listing": "NOT-PUBLISHED"}, {"unit": 77}])
def test_public_comparison_does_not_probe_forecasts_for_unpublished_private_identifiers(monkeypatch, selector):
    routes, calls = comparison_setup(monkeypatch)
    context = routes.comparison_context(scope="city", **selector)
    assert context["selected_offer"] is None
    assert calls == []
