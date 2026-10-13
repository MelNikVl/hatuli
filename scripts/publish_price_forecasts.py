#!/usr/bin/env python3
"""Offline training, validation and compact publication from forward captures."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.core.price_forecast import (
    METHOD_VERSION, atomic_write, forecast_listing, latest_record_index,
    load_observation_history, train_forecaster,
)


def publish(observations: Path, model_path: Path, output: Path, *, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    records = load_observation_history(observations, as_of=now)
    artifact = train_forecaster(records, as_of=now, source_history_verified=bool(records))
    atomic_write(model_path, artifact)
    ready = any(r["status"] == "ready" for r in artifact["horizons"])
    offers = {}
    if ready:
        for identifier, record in latest_record_index(records).items():
            points = record.get("points", [])
            if not points:
                continue
            latest = max(points, key=lambda p: p["captured_at"])
            if (now - datetime.fromisoformat(latest["source_observed_at"])).total_seconds() > 48 * 3600:
                continue
            price = latest["price"]
            forecast = forecast_listing({**record, "price": price}, artifact, now=now)
            offers[identifier] = {"price": price, "forecast": forecast}
    public = {"schema_version": 1, "method_version": METHOD_VERSION,
              "model_version": artifact["model_version"], "generated_at": now.isoformat(),
              "source_history_verified": artifact["source_history_verified"],
              "reports": [{"horizon_days": r["horizon_days"], "status": r["status"],
                           "validation": r["validation"], "fold_count": len(r["folds"])}
                          for r in artifact["horizons"]], "offers": offers}
    atomic_write(output, public)
    return {"generated_at": public["generated_at"], "audit": artifact["audit"],
            "published_offers": len(offers), "horizons": [
                {"days": r["horizon_days"], "status": r["status"]} for r in artifact["horizons"]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations-dir", type=Path, default=Path("var/price-forecast-observations"))
    parser.add_argument("--model", type=Path, default=Path("var/price-forecast-model.json"))
    parser.add_argument("--output", type=Path, default=Path("var/price-forecasts.json"))
    args = parser.parse_args()
    print(json.dumps(publish(args.observations_dir, args.model, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
