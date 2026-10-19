"""Small offline projections only; requests never train or load private history."""
from __future__ import annotations

import copy
import json
import math
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from bot.core.price_forecast import HORIZONS, METHOD_VERSION, forecast_listing, passes_gate

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "var" / "price-forecasts.json"
MAX_AGE = timedelta(hours=48)


def _utc(value):
    if not isinstance(value, str):
        raise ValueError("ISO timestamp required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("aware date required")
    return parsed.astimezone(timezone.utc)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_metrics(metrics, folds):
    if not isinstance(metrics, dict) or not _number(folds) or not float(folds).is_integer():
        return False
    keys = ("sample_size", "independent_properties", "changed_properties", "rms_log_error",
            "baseline_rms_log_error", "rms_log_improvement_pct", "mape_pct", "baseline_mape_pct",
            "coverage_pct", "median_interval_width_pct", "interval_nominal_coverage_pct")
    if not all(_number(metrics.get(k)) for k in keys):
        return False
    if not all(float(metrics[k]).is_integer() for k in keys[:3]):
        return False
    interval = metrics.get("mse_improvement_ci95")
    if not isinstance(interval, list) or len(interval) != 2 or not all(_number(v) for v in interval):
        return False
    return (metrics["sample_size"] >= metrics["independent_properties"] >= metrics["changed_properties"] >= 0
            and metrics["rms_log_error"] >= 0 and metrics["baseline_rms_log_error"] >= 0
            and 0 <= metrics["mape_pct"] <= 200 and 0 <= metrics["baseline_mape_pct"] <= 200
            and 0 <= metrics["coverage_pct"] <= 100 and 0 <= metrics["median_interval_width_pct"] <= 40
            and 0 <= metrics["rms_log_improvement_pct"] <= 100
            and metrics["interval_nominal_coverage_pct"] == 80 and interval[0] <= interval[1]
            and passes_gate(metrics, folds))


@lru_cache(maxsize=2)
def _read(path: str, modified: int, size: int) -> dict | None:
    try:
        if size > 80_000_000:
            return None
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if (data.get("schema_version") != 1 or data.get("method_version") != METHOD_VERSION
                or not isinstance(data.get("offers"), dict) or not isinstance(data.get("reports"), list)):
            return None
        _utc(data["generated_at"])
        return data
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def forecast_for_offer(identifier, price, *, now=None, path=None, active=True) -> dict:
    """Call only after the existing offer's access/source guard; no public lookup API."""
    now = now or datetime.now(timezone.utc)
    fallback = forecast_listing({"price": price}, None, now=now)
    if not active:
        for forecast in fallback["forecasts"]:
            forecast.update(status="inactive_listing", reason="Объявление не подтверждено как доступное. Прогноз снятой квартиры не показываем.")
        return fallback
    source = Path(path or os.environ.get("PRICE_FORECAST_PUBLIC_PATH") or DEFAULT_PATH)
    try:
        stat = source.stat()
        data = _read(str(source), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return fallback
    if data is None:
        return fallback
    generated = _utc(data["generated_at"])
    fresh = generated <= now <= generated + MAX_AGE
    reports = {r["horizon_days"]: r for r in data["reports"] if isinstance(r, dict)
               and _number(r.get("horizon_days")) and r["horizon_days"] in HORIZONS}
    stored = data["offers"].get(str(identifier), {})
    if not isinstance(stored, dict):
        stored = {}
    same_price = _number(price) and stored.get("price") == price
    projection = stored.get("forecast") if same_price else None
    if not isinstance(projection, dict):
        projection = None
    raw_candidates = (projection or {}).get("forecasts", [])
    if not isinstance(raw_candidates, list):
        raw_candidates = []
    candidates = {f["horizon_days"]: f for f in raw_candidates if isinstance(f, dict)
                  and _number(f.get("horizon_days")) and f["horizon_days"] in HORIZONS}
    result = copy.deepcopy(fallback)
    result["as_of"] = generated.isoformat()
    for output in result["forecasts"]:
        horizon = output["horizon_days"]
        report = reports.get(horizon, {})
        status = report.get("status", "missing_source_history")
        if not isinstance(status, str):
            status = "quality_gate_failed"
        if status == "ready":
            metrics = report.get("validation", {})
            try:
                valid_gate = (data.get("source_history_verified") is True
                    and _valid_metrics(metrics, report.get("fold_count", 0)))
            except (TypeError, ValueError, IndexError):
                valid_gate = False
            status = "insufficient_listing_history" if valid_gate else "quality_gate_failed"
            candidate = candidates.get(horizon)
            if valid_gate and candidate and candidate.get("status") == "ready":
                try:
                    numbers = [candidate[k] for k in ("low", "estimate", "high")]
                    anchor = _utc(candidate.get("as_of") or projection["as_of"])
                    target = datetime.fromisoformat(candidate["target_date"]).date()
                    valid = (all(_number(v) for v in numbers) and 1_000_000 <= numbers[0] <= numbers[1] <= numbers[2] <= 2_000_000_000
                        and .5 <= numbers[1] / price <= 1.5
                        and _number(candidate.get("historical_analog_count"))
                        and candidate["historical_analog_count"] >= 20
                        and generated.date() < target == anchor.date() + timedelta(days=horizon)
                        and generated - timedelta(days=2) <= anchor <= generated)
                except (KeyError, TypeError, ValueError):
                    valid = False
                if valid and fresh:
                    output.update({key: candidate.get(key) for key in (
                        "status", "estimate", "low", "high", "historical_analog_count", "target_date")})
                    for key in ("reason", "model_version"):
                        value = candidate.get(key)
                        output[key] = value if isinstance(value, str) and len(value) <= 1000 else None
                    output["validation"] = {k: metrics[k] for k in (
                        "sample_size", "independent_properties", "mape_pct", "baseline_mape_pct", "coverage_pct", "interval_nominal_coverage_pct") if k in metrics}
                    output["as_of"] = anchor.isoformat()
                    result["as_of"] = anchor.isoformat()
                    continue
            elif valid_gate and same_price and candidate and candidate.get("status") != "ready":
                status = candidate.get("status", "insufficient_listing_history")
                if not isinstance(status, str):
                    status = "quality_gate_failed"
        if not fresh:
            status = "stale_model"
        output["status"] = status
        from bot.core.price_forecast import _REASONS
        output["reason"] = _REASONS.get(status, fallback["forecasts"][0]["reason"])
    return result
