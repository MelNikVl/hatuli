"""Behavioral checks of temporal/source provenance, independent groups and gates.

No database connection, application startup, production fixtures or migrations.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import math

import pytest

from bot.core import price_forecast as pf

NOW = datetime(2026, 10, 13, 15, tzinfo=timezone.utc)
START = date(2026, 8, 14)


def point(day, price=30_000_000, **overrides):
    d = START + timedelta(days=day)
    observed = datetime.combine(d, datetime.min.time(), timezone.utc) + timedelta(hours=9)
    return {"date": d.isoformat(), "price": price, "source_observed_at": observed.isoformat(),
            "captured_at": (observed + timedelta(minutes=5)).isoformat(), "source_status": "active",
            "source_price_confirmed": True, **overrides}


def record(group="group-1", count=59, **overrides):
    return {"listing_id": "1000001", "group_id": group, "identity_grouping_verified": True,
            "rooms": 2, "area": 60, "attributes_known_at": "2026-08-14T08:00:00+00:00",
            "attribute_provenance": "versioned_source_observation", "market_type": "secondary",
            "hex_id": "0:0", "hex_edge_m": 100, "complex_id": 1,
            "points": [point(i) for i in range(count)],
            **overrides}


def ready_metrics(**changes):
    return {"independent_properties": 400, "changed_properties": 100, "rms_log_improvement_pct": 10,
            "mse_improvement_ci95": [.001, .003], "coverage_pct": 82, "median_interval_width_pct": 10,
            "mape_pct": 1.9, "baseline_mape_pct": 2.0, "sample_size": 600, **changes}


def ready_artifact(**changes):
    model_rows = []
    query = pf.features_at(pf.prepare_records([record()], NOW)[0][0], START + timedelta(days=58))
    for i in range(30):
        model_rows.append({"group_id": f"other-{i}", "rooms": 2, "area": 60, "price": 30_000_000,
                           "market_type": "secondary", "hex_id": "0:0", "complex_id": 1,
                           "x": query["x"], "y": -.02})
    artifact = {"schema_version": 1, "method_version": pf.METHOD_VERSION, "model_version": "test-1",
                "trained_at": NOW.isoformat(), "target": "future_asking_price_conditional_on_remaining_listed",
                "source_history_verified": True,
                "horizons": [{"horizon_days": 7, "status": "ready", "validation": ready_metrics(),
                              "folds": [{"start": 1}, {"start": 2}], "interval_radius": .03,
                              "model": {"scale": [1.0] * len(pf.FEATURES), "center": [0.0] * len(pf.FEATURES),
                                        "rows": model_rows}}]}
    artifact.update(changes)
    return artifact


def test_database_copy_timestamp_is_not_source_provenance():
    copied = point(10)
    copied["observed_at"] = copied.pop("source_observed_at")
    assert pf.checked_point(copied, NOW) is None


@pytest.mark.parametrize("overrides", [
    {"source_status": "archived"}, {"source_status": "unknown"}, {"source_price_confirmed": False},
    {"price": float("nan")}, {"price": True}, {"price": 0},
    {"source_observed_at": "2026-08-20T09:00:00"},
    {"source_observed_at": "2026-08-10T09:00:00+00:00"},
    {"source_observed_at": "2026-08-30T09:00:00+00:00"},
    {"captured_at": "2026-11-01T09:00:00+00:00"},
])
def test_rejects_stale_archived_unconfirmed_future_or_malformed_observations(overrides):
    assert pf.checked_point(point(10, **overrides), NOW) is None


def test_attributes_not_known_at_anchor_cannot_be_backfilled():
    raw = record(attributes_known_at="2026-09-01T09:00:00+00:00")
    prepared, audit = pf.prepare_records([raw], NOW)
    assert min(prepared[0]["points"]) == date(2026, 9, 1)
    assert audit["attributes_not_yet_known"] == 18
    assert pf.features_at(prepared[0], date(2026, 8, 29)) is None


def test_current_listing_metadata_is_not_a_historical_attribute_source():
    raw = record(attribute_provenance="current_listing_row")
    prepared, audit = pf.prepare_records([raw], NOW)
    assert not prepared
    assert audit["missing_attribute_history"] == 1


def test_bootstrap_identity_alone_is_not_accepted_as_relist_grouping():
    raw = record(identity_grouping_verified=False)
    prepared, audit = pf.prepare_records([raw], NOW)
    assert not prepared
    assert audit["missing_identity_groups"] == 1


def test_future_changed_price_and_current_metadata_do_not_change_past_features():
    raw = record()
    first, _ = pf.prepare_records([raw], NOW)
    before = pf.features_at(first[0], date(2026, 9, 1))
    raw["points"][40]["price"] = 10_000_000
    raw.update(renovation="designer", current_area=150, current_rooms=5, current_finish="rough")
    second, _ = pf.prepare_records([raw], NOW)
    assert pf.features_at(second[0], date(2026, 9, 1)) == before


def test_a_missing_target_is_censored_not_filled_with_last_known_price():
    raw = record(count=25)
    prepared, _ = pf.prepare_records([raw], NOW)
    examples = pf.build_examples(prepared, 7)
    anchor = examples[0]["anchor"]
    raw["points"] = [p for p in raw["points"] if p["date"] != (anchor + timedelta(days=7)).isoformat()]
    prepared, _ = pf.prepare_records([raw], NOW)
    assert anchor not in [r["anchor"] for r in pf.build_examples(prepared, 7)]


def test_price_cut_features_come_only_from_confirmed_prior_points():
    raw = record(count=25)
    for p in raw["points"][12:]:
        p["price"] = 27_000_000
    prepared, _ = pf.prepare_records([raw], NOW)
    row = pf.features_at(prepared[0], START + timedelta(days=14))
    assert row["x"][3] == pytest.approx(math.log(.9))
    assert row["x"][5] == 1


def groups_by_bucket(n=80):
    groups = {i: [] for i in range(10)}
    for index in range(n * 15):
        group = f"property-component-{index}"
        groups[pf.identity_bucket(group)].append(group)
    return groups


def test_rolling_split_has_disjoint_physical_groups_and_purged_labels():
    groups = groups_by_bucket(3)
    rows = []
    for bucket in range(10):
        group = groups[bucket][0]
        for day in range(40):
            anchor = START + timedelta(days=day)
            rows.append({"group_id": group, "anchor": anchor,
                         "target_available_at": datetime.combine(anchor + timedelta(days=7),
                         datetime.min.time(), timezone.utc) + timedelta(hours=12)})
    validation_start = START + timedelta(days=30)
    split = pf.split_fold(rows, validation_start, 7)
    sets = {name: {r["group_id"] for r in subset} for name, subset in split.items()}
    assert not sets["train"] & sets["calibration"]
    assert not sets["train"] & sets["test"]
    assert not sets["calibration"] & sets["test"]
    calibration_start = validation_start - timedelta(days=10)
    assert all(r["target_available_at"].date() < calibration_start for r in split["train"])
    assert all(r["target_available_at"].date() < validation_start for r in split["calibration"])
    assert len(split["test"]) == len(sets["test"])  # not repeated daily copies


def test_neighbours_deduplicate_groups_and_exclude_own_relists():
    query = {"group_id": "same-home", "rooms": 2, "area": 60, "price": 30_000_000,
             "hex_id": "0:0", "x": [0.0] * len(pf.FEATURES)}
    rows = [{**query, "group_id": f"home-{i}", "y": -.02} for i in range(25)]
    rows += [{**query, "y": .3} for _ in range(100)]
    rows += [rows[0]] * 100
    model = pf.fit_neighbours(rows)
    predicted, count = pf.predict_change(model, query)
    assert count == 25
    assert -.021 < predicted < -.015


def test_nominally_many_rows_cannot_replace_twenty_independent_analogs():
    query = {"group_id": "self", "rooms": 2, "area": 60, "price": 30_000_000,
             "hex_id": "0:0", "x": [0.0] * len(pf.FEATURES)}
    rows = [{**query, "group_id": "one-home", "y": -.02} for _ in range(200)]
    assert pf.predict_change(pf.fit_neighbours(rows), query) == (None, 1)


def test_apartment_rooms_area_and_price_define_a_real_analog_band():
    query = {"group_id": "self", "rooms": 2, "area": 60, "price": 30_000_000,
             "hex_id": "0:0", "x": [0.0] * len(pf.FEATURES)}
    rows = [{**query, "group_id": str(i), "y": -.02, "rooms": 3} for i in range(100)]
    rows += [{**query, "group_id": str(i + 100), "y": -.02, "area": 120} for i in range(100)]
    rows += [{**query, "group_id": str(i + 200), "y": -.02, "price": 60_000_000} for i in range(100)]
    assert pf.predict_change(pf.fit_neighbours(rows), query) == (None, 0)


def test_conformal_uses_finite_sample_rank_and_independent_sample_minimum():
    assert pf.conformal_quantile([.1] * 49) is None
    assert pf.conformal_quantile(list(range(50))) == 40  # ceil(51*.8), 1-indexed


@pytest.mark.parametrize("changes", [
    {"independent_properties": 199}, {"changed_properties": 49},
    {"rms_log_improvement_pct": 4.9}, {"mse_improvement_ci95": [0, .1]},
    {"coverage_pct": 74.9}, {"median_interval_width_pct": 40.1}, {"mape_pct": 2.2},
])
def test_predeclared_gate_cannot_pass_on_static_price_accuracy_alone(changes):
    assert not pf.passes_gate(ready_metrics(**changes), 2)


def test_gate_needs_multiple_future_periods():
    assert not pf.passes_gate(ready_metrics(), 1)
    assert pf.passes_gate(ready_metrics(), 2)


def test_no_phantom_bootstrap_gain_when_persistence_is_perfect():
    outcomes = [{"group_id": str(i), "y": 0.0, "prediction": -.01, "radius": .02} for i in range(300)]
    metrics = pf.evaluation_metrics(outcomes, bootstrap_samples=50)
    assert metrics["changed_properties"] == 0
    assert metrics["mse_improvement_ci95"][1] < 0
    assert not pf.passes_gate(metrics, 5)


def test_60_days_never_enables_yearly_or_30_day_forecasts():
    prepared, _ = pf.prepare_records([record()], NOW)
    for horizon in (30, 90, 365):
        result = pf.train_horizon(prepared, horizon, NOW)
        assert result["status"] == "insufficient_history"
        assert "model" not in result


def test_legacy_copied_history_is_an_explicit_abstention_not_a_zero_forecast():
    artifact = pf.train_forecaster([record()], as_of=NOW, source_history_verified=False)
    result = pf.forecast_listing(record(price=30_000_000), artifact, now=NOW)
    assert len(result["forecasts"]) == 5
    assert all(h["status"] == "missing_source_history" and h["estimate"] is None
               and h["low"] is None and h["high"] is None for h in result["forecasts"])


def test_public_projection_contains_no_training_groups_prices_contacts_or_addresses():
    raw = record(price=30_000_000, phone="PRIVATE_PHONE", address="PRIVATE_ADDRESS")
    result = pf.forecast_listing(raw, ready_artifact(), now=NOW)
    dumped = json.dumps(result)
    assert "PRIVATE" not in dumped and "group_id" not in dumped and "rows" not in dumped
    first = result["forecasts"][0]
    assert first["status"] == "ready"
    assert first["low"] < first["estimate"] < first["high"]
    assert first["historical_analog_count"] == 30
    assert result["forecasts"][-1]["estimate"] is None  # no annual extrapolation


def test_changed_current_price_requires_new_source_observation_before_forecast():
    result = pf.forecast_listing(record(price=31_000_000), ready_artifact(), now=NOW)
    assert result["forecasts"][0]["status"] == "insufficient_listing_history"
    assert result["forecasts"][0]["estimate"] is None


def test_stale_model_abstains_even_if_validation_previously_passed():
    result = pf.forecast_listing(record(price=30_000_000), ready_artifact(), now=NOW + timedelta(days=10))
    assert result["forecasts"][0]["status"] == "stale_model"


def test_ready_model_cannot_be_loaded_when_provenance_or_quality_gate_fails(tmp_path):
    for artifact in (ready_artifact(source_history_verified=False), ready_artifact()):
        if artifact["source_history_verified"]:
            artifact["horizons"][0]["validation"]["changed_properties"] = 2
        path = tmp_path / "model.json"
        pf.atomic_write(path, artifact)
        assert pf.load_artifact(path) is None


def test_model_loader_rejects_invalid_numeric_matrix_and_accepts_owned_json(tmp_path):
    path = tmp_path / "model.json"
    artifact = ready_artifact()
    pf.atomic_write(path, artifact)
    assert pf.load_artifact(path) == artifact
    artifact["horizons"][0]["model"]["scale"][0] = 0
    pf.atomic_write(path, artifact)
    assert pf.load_artifact(path) is None


def test_training_cli_has_no_database_or_application_startup_imports(tmp_path, capsys):
    from scripts.train_price_forecast import main
    input_file, output = tmp_path / "history.json", tmp_path / "model.json"
    input_file.write_text(json.dumps({"source_history_verified": False, "records": []}), encoding="utf-8")
    assert main(["--input", str(input_file), "--output", str(output), "--as-of", NOW.isoformat()]) == 0
    assert pf.load_artifact(output)["horizons"][0]["status"] == "missing_source_history"
    assert "missing_source_history" in capsys.readouterr().out


def test_one_day_of_real_new_collection_is_insufficient_history_not_missing_provenance():
    artifact = pf.train_forecaster([record(count=1)], as_of=NOW, source_history_verified=True)
    assert all(h["status"] == "insufficient_history" for h in artifact["horizons"])


def test_first_day_valid_cohort_keeps_its_history_reason_when_invalid_attributes_are_excluded():
    records = [record(count=1), record(group="excluded-outlier", count=1, area=900)]
    artifact = pf.train_forecaster(records, as_of=NOW, source_history_verified=True)
    assert artifact["audit"]["insufficient_source_points"] == 1
    assert artifact["audit"]["missing_attribute_history"] == 1
    assert all(h["status"] == "insufficient_history" for h in artifact["horizons"])


def test_no_valid_attribute_cohort_retains_the_missing_attribute_history_reason():
    artifact = pf.train_forecaster([record(count=1, area=900)], as_of=NOW, source_history_verified=True)
    assert all(h["status"] == "missing_attribute_history" for h in artifact["horizons"])


def test_prediction_target_is_anchored_to_latest_checked_quote_not_publication_date():
    result = pf.forecast_listing(record(price=30_000_000), ready_artifact(), now=NOW)
    assert result["as_of"].startswith("2026-10-11")
    assert result["forecasts"][0]["target_date"] == "2026-10-18"


def observation(identifier, day, **changes):
    return {"schema_version": 1, "record_type": "price_observation", "listing_id": identifier,
            "group_id": identifier, "purge_keys": [], "identity_grouping_verified": True,
            "rooms": 2, "area": 60, "market_type": "secondary", "source": "krisha",
            "hex_id": "0:0", "hex_edge_m": 100, "complex_id": 1,
            **point(day), **changes}


def write_observations(path, observations):
    path.write_text("\n".join(json.dumps(r) for r in observations) + "\n", encoding="utf-8")


def test_jsonl_reader_preserves_trait_versions_and_does_not_backfill_changes(tmp_path):
    write_observations(tmp_path / "capture.jsonl", [observation("1001", 1), observation("1001", 8),
                                                  observation("1001", 15, area=90, rooms=3)])
    records = pf.load_observation_history(tmp_path, as_of=NOW)
    assert len(records) == 2
    assert records[0]["area"] == 60 and records[1]["area"] == 90
    assert records[1]["attributes_known_at"] == point(15)["captured_at"]
    assert pf.latest_record_index(records)["1001"]["rooms"] == 3


def test_repeated_capture_of_the_same_source_check_is_not_another_history_point(tmp_path):
    earlier = observation("1001", 1)
    repeated = observation("1001", 2, source_observed_at=earlier["source_observed_at"])
    write_observations(tmp_path / "capture.jsonl", [earlier, repeated])
    records = pf.load_observation_history(tmp_path, as_of=NOW)
    assert len(records[0]["points"]) == 1


def test_later_identity_merge_purges_earlier_groups_without_creating_price_points(tmp_path):
    header = {"schema_version": 1, "record_type": "capture_meta", "captured_at": NOW.isoformat(),
              "group_links": [["1001", "2002"]]}
    write_observations(tmp_path / "a.jsonl", [observation("1001", 1), observation("2002", 1)])
    write_observations(tmp_path / "b.jsonl", [header])
    records = pf.load_observation_history(tmp_path, as_of=NOW)
    assert len(records) == 2
    assert records[0]["group_id"] == records[1]["group_id"]
    assert sum(len(r["points"]) for r in records) == 2


def test_future_collection_is_not_read_into_a_past_asof_training_set(tmp_path):
    write_observations(tmp_path / "capture.jsonl", [observation("1001", 1), observation("1001", 20)])
    cutoff = datetime(2026, 8, 25, tzinfo=timezone.utc)
    records = pf.load_observation_history(tmp_path, as_of=cutoff)
    assert len(records[0]["points"]) == 1


def test_reader_loads_real_collector_day_partitions(tmp_path):
    partition = tmp_path / "2026-08-15"
    partition.mkdir()
    write_observations(partition / "090500-capture.jsonl", [observation("1001", 1)])
    records = pf.load_observation_history(tmp_path, as_of=NOW)
    assert len(records) == 1 and len(records[0]["points"]) == 1


def test_same_rooms_area_primary_and_secondary_are_not_merged_as_analogs():
    query = {"group_id": "self", "rooms": 2, "area": 60, "price": 30_000_000,
             "market_type": "secondary", "hex_id": "0:0", "x": [0.0] * len(pf.FEATURES)}
    rows = [{**query, "group_id": str(i), "market_type": "primary", "y": -.02} for i in range(100)]
    assert pf.predict_change(pf.fit_neighbours(rows), query) == (None, 0)


def test_training_and_calibration_execute_on_real_rolling_structure(monkeypatch):
    # Small synthetic sample only for the exercise; the production gate and
    # its actual thresholds are separately tested above and are unchanged.
    monkeypatch.setattr(pf, "MIN_TRAIN_GROUPS", 20)
    monkeypatch.setattr(pf, "MIN_CALIBRATION_GROUPS", 10)
    monkeypatch.setattr(pf, "MIN_TEST_GROUPS", 10)
    monkeypatch.setattr(pf, "MIN_CHANGED_TEST_GROUPS", 5)
    synthetic = []
    for i in range(80):
        raw = record(group=f"synthetic-{i}", count=59)
        if i % 2:
            for d, p in enumerate(raw["points"]):
                p["price"] = round(30_000_000 * .98 ** (d // 7))
        synthetic.append(raw)
    prepared, _ = pf.prepare_records(synthetic, NOW)
    result = pf.train_horizon(prepared, 7, NOW)
    assert len(result["folds"]) >= 2
    assert result["validation"]["sample_size"] > 0
    assert result["validation"]["baseline_rms_log_error"] > 0
    assert result["status"] in {"ready", "quality_gate_failed"}


def test_an_apartment_in_another_district_is_not_a_historical_analog():
    query = {"group_id": "self", "rooms": 2, "area": 60, "price": 30_000_000,
             "hex_id": "0:0", "x": [0.0] * len(pf.FEATURES)}
    rows = [{**query, "group_id": str(i), "hex_id": "40:20", "y": -.02} for i in range(100)]
    assert pf.predict_change(pf.fit_neighbours(rows), query) == (None, 0)


def test_unknown_location_does_not_inherit_a_citywide_numeric_forecast():
    result = pf.forecast_listing(record(price=30_000_000, hex_id=None), ready_artifact(), now=NOW)
    assert result["forecasts"][0]["status"] == "missing_location_history"
    assert result["forecasts"][0]["estimate"] is None


def test_current_location_cannot_be_added_retroactively_to_a_property_creation_snapshot():
    prepared, audit = pf.prepare_records([record(attribute_provenance="immutable_property_creation")], NOW)
    assert not prepared and audit["missing_location_history"] == 1


def test_location_change_starts_a_new_known_version_without_overwriting_earlier_area(tmp_path):
    write_observations(tmp_path / "capture.jsonl", [observation("1001", 1), observation("1001", 8),
                                                  observation("1001", 15, hex_id="40:20", complex_id=2)])
    records = pf.load_observation_history(tmp_path, as_of=NOW)
    assert len(records) == 2
    assert records[0]["hex_id"] == "0:0" and records[1]["hex_id"] == "40:20"
    assert records[1]["attributes_known_at"] == point(15)["captured_at"]


def test_endpoint_prices_do_not_prove_a_continuous_listing_across_a_gap():
    raw = record(count=29)
    prepared, _ = pf.prepare_records([raw], NOW)
    row = pf.build_examples(prepared, 7)[0]
    removed = {row["anchor"] + timedelta(days=i) for i in (2, 3, 4)}
    raw["points"] = [p for p in raw["points"] if date.fromisoformat(p["date"]) not in removed]
    prepared, _ = pf.prepare_records([raw], NOW)
    assert row["anchor"] not in [r["anchor"] for r in pf.build_examples(prepared, 7)]


def test_a_reactivation_resets_the_training_episode_even_when_daily_endpoint_prices_exist(tmp_path):
    rows = [observation("1001", i, latest_reactivated_at=None if i < 13 else "2026-08-27T08:00:00+00:00")
            for i in range(29)]
    write_observations(tmp_path / "capture.jsonl", rows)
    records = pf.load_observation_history(tmp_path, as_of=NOW)
    assert len(records) == 2
    prepared, _ = pf.prepare_records(records, NOW)
    examples = pf.build_examples(prepared, 7)
    assert not any(row["anchor"] < START + timedelta(days=13) <= row["target_date"] for row in examples)


def test_a_novel_price_history_abstains_despite_many_similar_sized_neighbouring_apartments():
    query = {"group_id": "self", "rooms": 2, "area": 60, "price": 30_000_000,
             "hex_id": "0:0", "x": [0.0] * len(pf.FEATURES)}
    rows = [{**query, "group_id": str(i), "y": -.02} for i in range(100)]
    query["x"] = [0, 0, 0, -1.0, -1.0, 10, 0]
    assert pf.predict_change(pf.fit_neighbours(rows), query) == (None, 0)


def test_same_complex_is_preferred_when_enough_independent_historical_homes_exist():
    query = {"group_id": "self", "rooms": 2, "area": 60, "price": 30_000_000,
             "complex_id": 1, "hex_id": "0:0", "x": [0.0] * len(pf.FEATURES)}
    own = [{**query, "group_id": str(i), "y": -.02} for i in range(25)]
    other = [{**query, "group_id": str(i + 100), "complex_id": 2, "y": .2} for i in range(200)]
    prediction, count = pf.predict_change(pf.fit_neighbours(own + other), query)
    assert count == 25 and prediction < 0


def test_source_freshness_is_exactly_48_hours_from_actual_capture_time():
    raw = point(10)
    capture = datetime.fromisoformat(raw["captured_at"])
    raw["source_observed_at"] = (capture - timedelta(hours=47)).isoformat()
    assert pf.checked_point(raw, NOW) is not None
    raw["source_observed_at"] = (capture - timedelta(hours=49)).isoformat()
    assert pf.checked_point(raw, NOW) is None


def test_an_earlier_capture_cannot_be_assigned_a_later_forecast_anchor():
    raw = point(10)
    raw["date"] = (START + timedelta(days=11)).isoformat()
    assert pf.checked_point(raw, NOW) is None


def test_repeated_old_source_timestamp_is_not_a_continuously_observed_target():
    raw = record(count=29)
    prepared, _ = pf.prepare_records([raw], NOW)
    row = pf.build_examples(prepared, 7)[0]
    anchor_index = (row["anchor"] - START).days
    raw["points"][anchor_index + 1]["source_observed_at"] = raw["points"][anchor_index]["source_observed_at"]
    prepared, _ = pf.prepare_records([raw], NOW)
    assert row["anchor"] not in [r["anchor"] for r in pf.build_examples(prepared, 7)]
