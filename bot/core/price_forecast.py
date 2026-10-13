"""Historical-neighbour forecasts of future *asking* prices.

Training is offline. The legacy listing_snapshots table is a daily copy of
mutable database rows, so its observed_at must NEVER be treated as a source
observation. A usable point needs its own source_observed_at and immutable
attribute provenance. No transaction-price or annual-return extrapolation.

The gate is declared before fitting: purged chronological validation with
disjoint identity groups, >= 2 folds / 200 independent test properties / 50
changed-price outcomes; >= 5% RMS log-error improvement over persistence and
a positive group-bootstrap CI; 80% split-conformal intervals must cover at
least 75% of held-out outcomes. Failed models produce explicit abstentions.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

SCHEMA_VERSION = 1
METHOD_VERSION = "historical-neighbours-v2"
HORIZONS = (7, 14, 30, 90, 365)
INTERVAL_COVERAGE = .80
MIN_TRAIN_GROUPS = 100
MIN_CALIBRATION_GROUPS = 50
MIN_TEST_GROUPS = 200
MIN_CHANGED_TEST_GROUPS = 50
MIN_FOLDS = 2
MIN_ANALOGS = 20
MAX_ANALOGS = 60
TRAIN_WINDOW_DAYS = 7
CALIBRATION_WINDOW_DAYS = 3
TEST_WINDOW_DAYS = 3
FEATURE_LAG_DAYS = 7
MAX_SOURCE_LAG = timedelta(days=2)
MAX_MODEL_AGE = timedelta(days=8)
HEX_EDGE_M = 100
MAX_HEX_DISTANCE = 10
MAX_ANALOG_DISTANCE_SQ = 16.0
FEATURES = ("log_price", "log_area", "log_age", "return7", "return14", "cuts14", "days_since_change")
TARGET_LABEL = "Цена предложения при сохранении объявления в продаже"
LIMITATIONS = [
    "Это прогноз цены объявления, не цены состоявшейся сделки.",
    "Прогноз применим при сохранении квартиры в продаже; исчезновение не означает продажу.",
    "Исторические аналоги: тот же ближайший район, те же комнаты, площадь ±15%, цена ±35% и похожая история цены.",
    "Район ограничен 10 ячейками сетки с ребром 100 м (примерно 1,7–2 км); координаты могут быть приблизительными.",
    "По первичке точка может относиться ко всему проекту. Ремонт и условия продажи могут отличаться: их история пока не подтверждена.",
]
_REASONS = {
    "insufficient_history": "Не хватает истории для отдельного обучения, калибровки и проверки этого срока.",
    "missing_source_history": "Исторические снимки не подтверждают актуальность цены на источнике в каждый день.",
    "missing_attribute_history": "Не подтверждено, какие характеристики квартиры были известны в прошлом.",
    "missing_location_history": "Нет подтверждённой истории расположения квартиры для сравнения в ближайшем районе.",
    "insufficient_groups": "Недостаточно независимых квартир после разделения повторных объявлений.",
    "quality_gate_failed": "Модель ещё не прошла проверку на будущем периоде против сохранения текущей цены.",
    "insufficient_analogs": "Недостаточно независимых похожих квартир с проверяемым историческим исходом.",
    "stale_model": "Модель требует обновления и повторной проверки.",
    "insufficient_listing_history": "Для этого объявления ещё не накоплена подтверждённая история цены.",
}


def _day(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Source observations require an aware timestamp")
    return value.astimezone(timezone.utc)


def _finite(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _hex(value) -> tuple[int, int] | None:
    if not isinstance(value, str) or not re.fullmatch(r"-?\d{1,4}:-?\d{1,4}", value):
        return None
    q, r = map(int, value.split(":"))
    return (q, r) if max(abs(q), abs(r)) <= 2000 else None


def hex_distance(first: str, second: str) -> int | None:
    a, b = _hex(first), _hex(second)
    if a is None or b is None:
        return None
    dq, dr = a[0] - b[0], a[1] - b[1]
    return max(abs(dq), abs(dr), abs(dq + dr))


def identity_bucket(group_id: str) -> int:
    """Fixed group allocation across *all* time folds; IDs never public."""
    return int.from_bytes(hashlib.sha256(str(group_id).encode()).digest()[:8], "big") % 10


def checked_point(point: dict, as_of: datetime) -> dict | None:
    """Reject cached copies, archived prices, future observations and bad money."""
    try:
        day = _day(point["date"])
        observed = _utc(point["source_observed_at"])
        captured = _utc(point["captured_at"])
        price = _finite(point["price"])
    except (KeyError, ValueError, TypeError):
        return None
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone.utc)
    if (point.get("source_status") != "active" or point.get("source_price_confirmed") is not True
            or not price or not 5_000_000 <= price <= 600_000_000
            or not captured - MAX_SOURCE_LAG <= observed <= captured or captured > as_of
            or not end - timedelta(days=1) <= captured < end):
        return None
    # captured_at, not a retrospectively copied first_seen, limits features.
    return {"date": day, "price": price, "source_observed_at": observed, "captured_at": captured}


def prepare_records(records: list[dict], as_of: datetime) -> tuple[list[dict], dict]:
    """Only timestamped initial property traits; mutable listing metadata ignored.

    Each record's group_id must be the connected component of known relists,
    duplicate-photo hashes and confirmed identities, not a bootstrap property
    ID alone. The trusted collector declares identity_grouping_verified.
    """
    output, counters = [], defaultdict(int)
    for record in records:
        counters["input_listings"] += 1
        if record.get("identity_grouping_verified") is not True or not record.get("group_id"):
            counters["missing_identity_groups"] += 1
            continue
        rooms, area = _finite(record.get("rooms")), _finite(record.get("area"))
        try:
            known = _utc(record["attributes_known_at"])
        except (KeyError, TypeError, ValueError):
            counters["missing_attribute_history"] += 1
            continue
        if (record.get("attribute_provenance") not in {"immutable_property_creation", "versioned_source_observation"}
                or rooms is None or not rooms.is_integer() or not 1 <= rooms <= 6
                or area is None or not 20 <= area <= 300):
            counters["missing_attribute_history"] += 1
            continue
        points = {}
        for raw in record.get("points", []):
            counters["input_points"] += 1
            point = checked_point(raw, as_of)
            if point is None:
                counters["unconfirmed_source_points"] += 1
                continue
            if point["captured_at"] < known:
                counters["attributes_not_yet_known"] += 1
                continue
            previous = points.get(point["date"])
            if previous is None or previous["captured_at"] < point["captured_at"]:
                points[point["date"]] = point
        if len(points) < 3:
            counters["insufficient_source_points"] += 1
            continue
        market = record.get("market_type") if record.get("attribute_provenance") == "versioned_source_observation" else "unknown"
        if market not in {"primary", "secondary", "unknown"}:
            counters["missing_attribute_history"] += 1
            continue
        location = record.get("hex_id") if record.get("attribute_provenance") == "versioned_source_observation" else None
        if _hex(location) is None or record.get("hex_edge_m") != HEX_EDGE_M:
            counters["missing_location_history"] += 1
            continue
        complex_id = _finite(record.get("complex_id"))
        complex_id = int(complex_id) if complex_id is not None and complex_id.is_integer() and complex_id > 0 else None
        output.append({"listing_id": str(record.get("listing_id", "")), "group_id": str(record["group_id"]),
                       "rooms": int(rooms), "area": area, "market_type": market,
                       "hex_id": location, "hex_edge_m": HEX_EDGE_M, "complex_id": complex_id,
                       "known_at": known, "points": points})
    counters["usable_listings"] = len(output)
    counters["usable_points"] = sum(len(r["points"]) for r in output)
    return output, dict(counters)


def features_at(record: dict, anchor: date) -> dict | None:
    """All price history and attribute availability are cut off at anchor."""
    points = record["points"]
    current = points.get(anchor)
    if current is None:
        return None
    cutoff = current["captured_at"]
    history = sorted((p for d, p in points.items() if d <= anchor and p["captured_at"] <= cutoff),
                     key=lambda p: p["date"])
    if len(history) < 3 or (anchor - history[0]["date"]).days < FEATURE_LAG_DAYS:
        return None
    price = current["price"]
    def lag_return(lag):
        past = [p for p in history if p["date"] <= anchor - timedelta(days=lag)]
        return math.log(price / past[-1]["price"]) if past else 0.0
    recent = [p for p in history if p["date"] >= anchor - timedelta(days=14)]
    changes = [b for a, b in zip(recent, recent[1:]) if b["price"] != a["price"]]
    cuts = sum(b["price"] < a["price"] for a, b in zip(recent, recent[1:]))
    age = (anchor - history[0]["date"]).days
    days_since = (anchor - changes[-1]["date"]).days if changes else 15
    return {"group_id": record["group_id"], "listing_id": record["listing_id"],
            "anchor": anchor, "anchor_available_at": cutoff, "price": price,
            "rooms": record["rooms"], "area": record["area"], "market_type": record["market_type"],
            "hex_id": record["hex_id"], "complex_id": record["complex_id"],
            "x": [math.log(price), math.log(record["area"]), math.log1p(age),
                  lag_return(7), lag_return(14), float(cuts), math.log1p(days_since)]}


def build_examples(records: list[dict], horizon_days: int) -> list[dict]:
    """A missing future source check is censored, never filled with old price."""
    output = []
    for record in records:
        for day in sorted(record["points"]):
            if day.toordinal() % 3:
                continue
            row = features_at(record, day)
            future = record["points"].get(day + timedelta(days=horizon_days))
            if row is None or future is None:
                continue
            window = sorted((p for d, p in record["points"].items() if day <= d <= future["date"]),
                            key=lambda p: p["date"])
            if any((b["date"] - a["date"]).days > 2
                   or b["source_observed_at"] - a["source_observed_at"] > MAX_SOURCE_LAG
                   or b["source_observed_at"] <= a["source_observed_at"]
                   for a, b in zip(window, window[1:])):
                continue  # gaps/possible withdrawal are censored, not unchanged
            output.append({**row, "target_date": future["date"], "target_available_at": future["captured_at"],
                           "future_price": future["price"], "y": math.log(future["price"] / row["price"])})
    return output


def split_fold(examples: list[dict], validation_start: date, horizon_days: int) -> dict[str, list[dict]]:
    """Labels must finish before the next period begins, plus group embargo."""
    calibration_start = validation_start - timedelta(days=horizon_days + CALIBRATION_WINDOW_DAYS)
    calibration_end = calibration_start + timedelta(days=CALIBRATION_WINDOW_DAYS)
    validation_end = validation_start + timedelta(days=TEST_WINDOW_DAYS)
    boundary = lambda day: datetime.combine(day, datetime.min.time(), timezone.utc)
    train, calibration, test = [], [], []
    for row in examples:
        bucket = identity_bucket(row["group_id"])
        if (bucket < 6 and row["target_available_at"] < boundary(calibration_start)
                and row["anchor"] < calibration_start):
            train.append(row)
        elif (6 <= bucket < 8 and calibration_start <= row["anchor"] < calibration_end
              and row["target_available_at"] < boundary(validation_start)):
            calibration.append(row)
        elif bucket >= 8 and validation_start <= row["anchor"] < validation_end:
            test.append(row)
    # One evaluation row per physical group in a fold; training neighbours
    # also deduplicate by group at prediction, so long exposure is not n homes.
    def unique(rows):
        return list({r["group_id"]: r for r in sorted(rows, key=lambda r: r["anchor"])}.values())
    return {"train": train, "calibration": unique(calibration), "test": unique(test)}


def fit_neighbours(rows: list[dict]) -> dict:
    import numpy as np
    matrix = np.asarray([r["x"] for r in rows], dtype=float)
    center = np.median(matrix, axis=0)
    scale = np.maximum(np.median(np.abs(matrix - center), axis=0) * 1.4826,
                       np.array([.25, .20, .50, .025, .035, 1.0, .50]))
    return {"center": center.tolist(), "scale": scale.tolist(),
            "rows": [{"group_id": r["group_id"], "rooms": r["rooms"], "area": r["area"],
                      "market_type": r.get("market_type", "unknown"),
                      "hex_id": r.get("hex_id"), "complex_id": r.get("complex_id"),
                      "price": r["price"], "x": r["x"], "y": r["y"]} for r in rows]}


def predict_change(model: dict, query: dict) -> tuple[float | None, int]:
    """Independent historical apartments, no own-listing or future examples."""
    import numpy as np
    if _hex(query.get("hex_id")) is None:
        return None, 0
    eligible = [r for r in model["rows"] if r["group_id"] != query["group_id"]
                and r.get("market_type", "unknown") == query.get("market_type", "unknown")
                and hex_distance(r.get("hex_id"), query["hex_id"]) is not None
                and hex_distance(r["hex_id"], query["hex_id"]) <= MAX_HEX_DISTANCE
                and r["rooms"] == query["rooms"] and abs(r["area"] / query["area"] - 1) <= .15
                and abs(r["price"] / query["price"] - 1) <= .35]
    if not eligible:
        return None, 0
    if query.get("complex_id") is not None:
        same_complex = [r for r in eligible if r.get("complex_id") == query["complex_id"]]
        if len({r["group_id"] for r in same_complex}) >= MIN_ANALOGS:
            eligible = same_complex
    matrix = np.asarray([r["x"] for r in eligible], dtype=float)
    distance = (np.sum(((matrix - query["x"]) / model["scale"]) ** 2, axis=1)
                + np.array([(hex_distance(r["hex_id"], query["hex_id"]) / MAX_HEX_DISTANCE) ** 2 for r in eligible]))
    order = np.argsort(distance, kind="stable")
    selected, groups = [], set()
    for ix in order:
        if distance[int(ix)] > MAX_ANALOG_DISTANCE_SQ:
            break  # no numerical extrapolation into a novel price trajectory
        group = eligible[int(ix)]["group_id"]
        if group in groups:
            continue
        groups.add(group)
        selected.append(int(ix))
        if len(selected) >= MAX_ANALOGS:
            break
    if len(selected) < MIN_ANALOGS:
        return None, len(selected)
    weights = 1 / (1 + distance[selected])
    values = np.array([eligible[ix]["y"] for ix in selected])
    # A predeclared mild shrinkage prevents sparse noisy neighbours implying
    # automatic appreciation. It is not a manually supplied growth rate.
    estimate = float(np.sum(values * weights) / (np.sum(weights) + 3.0))
    return max(math.log(.5), min(math.log(1.5), estimate)), len(selected)


def conformal_quantile(errors: list[float], coverage: float = INTERVAL_COVERAGE) -> float | None:
    """Finite-sample split-conformal rank, on independent calibration groups."""
    if len(errors) < MIN_CALIBRATION_GROUPS:
        return None
    values = sorted(abs(float(e)) for e in errors)
    rank = min(len(values), math.ceil((len(values) + 1) * coverage))
    return values[rank - 1]


def _rms(values: list[float]) -> float:
    return math.sqrt(sum(v * v for v in values) / len(values))


def evaluation_metrics(outcomes: list[dict], *, bootstrap_samples: int = 400) -> dict:
    """Cluster resampling respects the same apartment across rolling folds."""
    import numpy as np
    if not outcomes:
        return {}
    errors = [r["y"] - r["prediction"] for r in outcomes]
    baseline = [r["y"] for r in outcomes]  # persistence: tomorrow == now
    model_rms, base_rms = _rms(errors), _rms(baseline)
    groups = defaultdict(list)
    for r, err in zip(outcomes, errors):
        groups[r["group_id"]].append((r["y"] ** 2 - err ** 2))
    values = np.array([sum(v) / len(v) for v in groups.values()])
    rng = np.random.default_rng(20261013)
    differences = [float(np.mean(values[rng.integers(0, len(values), len(values))]))
                   for _ in range(bootstrap_samples)]
    low, high = np.quantile(differences, [.025, .975])
    mape = sum(abs(math.expm1(-e)) for e in errors) / len(errors) * 100
    baseline_mape = sum(abs(math.expm1(-e)) for e in baseline) / len(baseline) * 100
    changed = {r["group_id"] for r in outcomes if abs(r["y"]) >= math.log(1.01)}
    return {"sample_size": len(outcomes), "independent_properties": len(groups), "changed_properties": len(changed),
            "rms_log_error": model_rms, "baseline_rms_log_error": base_rms,
            "rms_log_improvement_pct": (1 - model_rms / base_rms) * 100 if base_rms else 0,
            "mape_pct": mape, "baseline_mape_pct": baseline_mape,
            "coverage_pct": sum(abs(e) <= r["radius"] for e, r in zip(errors, outcomes)) / len(errors) * 100,
            "mse_improvement_ci95": [float(low), float(high)],
            "interval_nominal_coverage_pct": INTERVAL_COVERAGE * 100,
            "median_interval_width_pct": median((math.exp(r["radius"]) - math.exp(-r["radius"])) * 100
                                                  for r in outcomes)}


def passes_gate(metrics: dict, folds: int) -> bool:
    return bool(metrics and folds >= MIN_FOLDS and metrics.get("independent_properties", 0) >= MIN_TEST_GROUPS
                and metrics.get("changed_properties", 0) >= MIN_CHANGED_TEST_GROUPS
                and metrics.get("rms_log_improvement_pct", 0) >= 5
                and metrics.get("mse_improvement_ci95", [0])[0] > 0
                and metrics.get("coverage_pct", 0) >= 75
                and metrics.get("median_interval_width_pct", 100) <= 40
                and metrics.get("mape_pct", 100) <= metrics.get("baseline_mape_pct", 0) * 1.05)


def train_horizon(records: list[dict], horizon_days: int, as_of: datetime) -> dict:
    points = [d for r in records for d in r["points"]]
    result = {"horizon_days": horizon_days, "status": "insufficient_history", "validation": {}, "folds": []}
    if not points:
        return result
    first, last = min(points), min(max(points), as_of.date())
    minimum = (FEATURE_LAG_DAYS + TRAIN_WINDOW_DAYS + 3 * horizon_days
               + CALIBRATION_WINDOW_DAYS + TEST_WINDOW_DAYS + TEST_WINDOW_DAYS * (MIN_FOLDS - 1))
    result["history_days"] = (last - first).days + 1
    result["minimum_history_days"] = minimum + 1
    if (last - first).days < minimum:
        return result
    examples = build_examples(records, horizon_days)
    validation_start = first + timedelta(days=FEATURE_LAG_DAYS + TRAIN_WINDOW_DAYS
                                        + 2 * horizon_days + CALIBRATION_WINDOW_DAYS)
    outcomes = []
    while validation_start + timedelta(days=TEST_WINDOW_DAYS + horizon_days) <= last:
        split = split_fold(examples, validation_start, horizon_days)
        counts = {k: len({r["group_id"] for r in rows}) for k, rows in split.items()}
        if counts["train"] >= MIN_TRAIN_GROUPS and counts["calibration"] >= MIN_CALIBRATION_GROUPS:
            model = fit_neighbours(split["train"])
            calibration = []
            for row in split["calibration"]:
                p, _ = predict_change(model, row)
                if p is not None:
                    calibration.append(abs(row["y"] - p))
            radius = conformal_quantile(calibration)
            tested = []
            if radius is not None:
                for row in split["test"]:
                    p, n = predict_change(model, row)
                    if p is not None:
                        tested.append({"group_id": row["group_id"], "y": row["y"], "prediction": p,
                                       "radius": radius, "analogs": n})
            if tested:
                outcomes.extend(tested)
                result["folds"].append({"validation_start": validation_start.isoformat(), **counts,
                                        "calibration_used": len(calibration), "test_used": len(tested)})
        validation_start += timedelta(days=TEST_WINDOW_DAYS)
    result["validation"] = evaluation_metrics(outcomes)
    result["status"] = "quality_gate_failed"
    if not passes_gate(result["validation"], len(result["folds"])):
        return result
    # Final fitting also keeps the calibration groups and their labels out.
    final_start = last - timedelta(days=horizon_days)
    split = split_fold(examples, final_start, horizon_days)
    model = fit_neighbours(split["train"])
    residuals = []
    for row in split["calibration"]:
        p, _ = predict_change(model, row)
        if p is not None:
            residuals.append(abs(row["y"] - p))
    radius = conformal_quantile(residuals)
    if radius is None:
        result["status"] = "insufficient_groups"
        return result
    result.update(status="ready", model=model, interval_radius=radius)
    return result


def train_forecaster(records: list[dict], *, as_of: datetime | None = None,
                     horizons=HORIZONS, source_history_verified: bool = False) -> dict:
    as_of = _utc(as_of or datetime.now(timezone.utc))
    prepared, audit = prepare_records(records, as_of)
    forecast_horizons = []
    for horizon in horizons:
        if horizon not in HORIZONS:
            raise ValueError("Unsupported forecast horizon")
        if not source_history_verified or not prepared:
            status = "missing_source_history"
            if source_history_verified:
                if audit.get("insufficient_source_points"):
                    # A growing valid cohort is not globally invalidated by
                    # a minority of excluded out-of-domain attribute rows.
                    status = "insufficient_history"
                elif audit.get("missing_attribute_history"):
                    status = "missing_attribute_history"
                elif audit.get("missing_identity_groups"):
                    status = "insufficient_groups"
                elif audit.get("missing_location_history"):
                    status = "missing_location_history"
                elif not audit.get("unconfirmed_source_points"):
                    status = "insufficient_history"
            forecast_horizons.append({"horizon_days": horizon, "status": status, "validation": {}, "folds": []})
        else:
            forecast_horizons.append(train_horizon(prepared, horizon, as_of))
    return {"schema_version": SCHEMA_VERSION, "method_version": METHOD_VERSION,
            "model_version": as_of.strftime("%Y%m%dT%H%M%SZ"), "trained_at": as_of.isoformat(),
            "target": "future_asking_price_conditional_on_remaining_listed", "target_label": TARGET_LABEL,
            "source_history_verified": source_history_verified, "audit": audit,
            "gate": {"min_folds": MIN_FOLDS, "min_test_properties": MIN_TEST_GROUPS,
                     "min_changed_properties": MIN_CHANGED_TEST_GROUPS, "min_rms_log_improvement_pct": 5,
                     "interval_nominal_coverage_pct": INTERVAL_COVERAGE * 100, "min_empirical_coverage_pct": 75,
                     "hex_edge_m": HEX_EDGE_M, "max_hex_distance": MAX_HEX_DISTANCE,
                     "max_analog_distance_sq": MAX_ANALOG_DISTANCE_SQ},
            "horizons": forecast_horizons}


def forecast_listing(listing: dict, artifact: dict | None, *, now: datetime | None = None) -> dict:
    """Public projection; never expose training rows, identity groups or IDs."""
    now = _utc(now or datetime.now(timezone.utc))
    prepared, listing_audit = prepare_records([listing], now)
    record = prepared[0] if prepared else None
    latest_day = max(record["points"]) if record else None
    query = features_at(record, latest_day) if record else None
    current_price = _finite(listing.get("price"))
    if query and current_price and query["price"] != current_price:
        query = None  # Last checked observation no longer matches this offer.
    forecasts = []
    reports = {h["horizon_days"]: h for h in (artifact or {}).get("horizons", [])}
    trained = None
    try:
        trained = _utc((artifact or {})["trained_at"])
    except (ValueError, TypeError, KeyError):
        pass
    for horizon in HORIZONS:
        report = reports.get(horizon, {})
        status = report.get("status", "missing_source_history")
        if status == "ready" and (artifact or {}).get("source_history_verified") is not True:
            status = "missing_source_history"
        if status == "ready" and (trained is None or not trained <= now <= trained + MAX_MODEL_AGE):
            status = "stale_model"
        if status == "ready" and (not query or not latest_day or (now.date() - latest_day).days > 2):
            status = "missing_location_history" if listing_audit.get("missing_location_history") else "insufficient_listing_history"
        p, count = None, 0
        if status == "ready":
            p, count = predict_change(report["model"], query)
            if p is None:
                status = "insufficient_analogs"
        estimate, low, high = None, None, None
        if status == "ready":
            radius = report["interval_radius"]
            estimate = round(query["price"] * math.exp(p))
            low, high = round(query["price"] * math.exp(p - radius)), round(query["price"] * math.exp(p + radius))
        validation = report.get("validation", {})
        # Values are empirical reports, no invented confidence score.
        forecasts.append({"status": status, "horizon_days": horizon, "estimate": estimate,
                          "low": low, "high": high, "historical_analog_count": count,
                          "target_date": ((latest_day if query else now.date()) + timedelta(days=horizon)).isoformat(),
                          "reason": _REASONS.get(status, "На исторических похожих квартирах."),
                          "model_version": (artifact or {}).get("model_version"),
                          "validation": {k: validation[k] for k in ("sample_size", "independent_properties", "mape_pct",
                                         "baseline_mape_pct", "coverage_pct", "interval_nominal_coverage_pct") if k in validation}})
    return {"as_of": query["anchor_available_at"].isoformat() if query else now.isoformat(),
            "methodology_label": TARGET_LABEL,
            "forecasts": forecasts, "limitations": list(LIMITATIONS)}


def atomic_write(path: Path, artifact: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            json.dump(artifact, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def load_observation_history(directory: Path, *, as_of: datetime | None = None) -> list[dict]:
    """Read private append-only collector JSONL, never database copies.

    Later dedup evidence unions earlier groups across every capture. Attribute
    changes start a new availability interval; we neither overwrite earlier
    rooms/area nor fill an earlier date with the current listing card.
    """
    as_of = _utc(as_of or datetime.now(timezone.utc))
    parent, rows = {}, []
    def find(key):
        parent.setdefault(key, key)
        root = key
        while parent[root] != root:
            root = parent[root]
        while parent[key] != key:
            old = parent[key]
            parent[key] = root
            key = old
        return root
    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            # Deterministic root independent of input file order.
            parent[max(a, b)] = min(a, b)
    for path in sorted(Path(directory).rglob("*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                raw = json.loads(line)
                if raw.get("schema_version") != 1:
                    raise ValueError("Unsupported private observation schema")
                captured = _utc(raw["captured_at"])
                if captured > as_of:
                    continue
                if raw.get("record_type") == "capture_meta":
                    for pair in raw.get("group_links", []):
                        if isinstance(pair, list) and len(pair) == 2 and all(isinstance(v, str) and v for v in pair):
                            union("group:" + pair[0], "group:" + pair[1])
                    continue
                identifier = str(raw["listing_id"])
                if not identifier or raw.get("identity_grouping_verified") is not True:
                    continue
                root_key = "listing:" + identifier
                find(root_key)
                # These are private salted/hashed purge signals, never public
                # names, addresses or seller contacts.
                for key in raw.get("purge_keys", []):
                    if isinstance(key, str) and key:
                        union(root_key, "purge:" + key)
                if raw.get("group_id"):
                    union(root_key, "group:" + str(raw["group_id"]))
                rows.append({**raw, "date": captured.date().isoformat()})
    by_listing = defaultdict(list)
    for row in rows:
        by_listing[str(row["listing_id"])].append(row)
    records = []
    for identifier, observations in by_listing.items():
        observations.sort(key=lambda r: (_utc(r["captured_at"]), _utc(r["source_observed_at"])))
        current, signature, seen_sources = None, None, set()
        for raw in observations:
            source_key = (raw.get("source"), raw["source_observed_at"])
            if source_key in seen_sources:
                continue  # repeated captures of one old source check ≠ history
            seen_sources.add(source_key)
            traits = (raw.get("rooms"), raw.get("area"), raw.get("market_type"),
                      raw.get("hex_id"), raw.get("hex_edge_m"), raw.get("complex_id"),
                      raw.get("latest_reactivated_at"))
            if traits != signature:
                current = {"listing_id": identifier, "group_id": find("listing:" + identifier),
                           "identity_grouping_verified": True, "rooms": traits[0], "area": traits[1],
                           "market_type": traits[2], "attributes_known_at": raw["captured_at"],
                           "hex_id": traits[3], "hex_edge_m": traits[4], "complex_id": traits[5],
                           "latest_reactivated_at": traits[6],
                           "attribute_provenance": "versioned_source_observation", "points": []}
                records.append(current)
                signature = traits
            current["points"].append({k: raw[k] for k in ("date", "price", "source_observed_at", "captured_at",
                                                          "source_status", "source_price_confirmed")})
    return records


def latest_record_index(records: list[dict]) -> dict[str, dict]:
    """Current version per listing; preserves frozen features for old versions."""
    result = {}
    for record in records:
        identifier = record["listing_id"]
        if record.get("points") and (identifier not in result
                or max(_utc(p["captured_at"]) for p in record["points"]) > max(_utc(p["captured_at"]) for p in result[identifier]["points"])):
            result[identifier] = record
    return result


def load_artifact(path: Path) -> dict | None:
    """Only locally owned JSON; no pickle or executable model loading."""
    try:
        artifact = json.loads(Path(path).read_text(encoding="utf-8"))
        if (artifact.get("schema_version") != SCHEMA_VERSION or artifact.get("method_version") != METHOD_VERSION
                or artifact.get("target") != "future_asking_price_conditional_on_remaining_listed"):
            return None
        _utc(artifact["trained_at"])
        for report in artifact["horizons"]:
            if report["horizon_days"] not in HORIZONS:
                return None
            if report["status"] == "ready":
                if not artifact.get("source_history_verified") or not passes_gate(report["validation"], len(report["folds"])):
                    return None
                if _finite(report["interval_radius"]) is None or report["interval_radius"] < 0:
                    return None
                model = report["model"]
                if len(model["scale"]) != len(FEATURES) or any(_finite(v) is None or v <= 0 for v in model["scale"]):
                    return None
                for row in model["rows"]:
                    if len(row["x"]) != len(FEATURES) or any(_finite(v) is None for v in row["x"]):
                        return None
                    if _finite(row["y"]) is None:
                        return None
                    if _hex(row.get("hex_id")) is None:
                        return None
                    if (not isinstance(row.get("group_id"), str) or not row["group_id"]
                            or _finite(row.get("price")) is None or not 5_000_000 <= row["price"] <= 600_000_000
                            or _finite(row.get("area")) is None or not 20 <= row["area"] <= 300
                            or _finite(row.get("rooms")) is None or row["rooms"] not in range(1, 7)):
                        return None
        return artifact
    except (OSError, json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError):
        return None
