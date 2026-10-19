#!/usr/bin/env python3
"""Train from timestamped, locally collected evidence; never connect to PG.

Input: {source_history_verified: bool, records: [..]} using the strict contract
in bot.core.price_forecast. Legacy database-copy snapshots are not source
evidence. A failed gate writes a report with abstentions, never a ready model.

  python scripts/train_price_forecast.py --input var/price-forecast-history.json \
      --output var/price-forecast-model.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.core.price_forecast import HORIZONS, atomic_write, load_observation_history, train_forecaster


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--input", type=Path)
    sources.add_argument("--observations-dir", type=Path, help="Private immutable collector JSONL directory")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--as-of", type=lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
                        default=datetime.now(timezone.utc))
    parser.add_argument("--horizon", type=int, action="append", choices=HORIZONS)
    args = parser.parse_args(argv)
    raw = (json.loads(args.input.read_text(encoding="utf-8")) if args.input else
           {"source_history_verified": True, "records": load_observation_history(args.observations_dir, as_of=args.as_of)})
    artifact = train_forecaster(raw.get("records", []), as_of=args.as_of, horizons=args.horizon or HORIZONS,
                                source_history_verified=raw.get("source_history_verified") is True)
    atomic_write(args.output, artifact)
    print(json.dumps({"trained_at": artifact["trained_at"], "audit": artifact["audit"],
                      "horizons": [{k: v for k, v in h.items() if k != "model"} for h in artifact["horizons"]]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
