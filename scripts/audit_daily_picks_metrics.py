#!/usr/bin/env python3
"""Aggregate-only audit of a saved daily edition; no database or network I/O.

Run anywhere with ``--root /home/nik/krisha_bot``. Only the requested JSON file
is read. No dotenv, database pools, collectors, publishers or migrations run.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from statistics import median
import sys

FACTORS = ("price", "price_history", "ceiling", "kitchen", "owner", "floor")


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp needs timezone")
    return result.astimezone(timezone.utc)


def numeric_summary(values):
    valid = [float(value) for value in values
             if type(value) in (int, float) and math.isfinite(value)]
    return {"count": len(valid), "invalid_count": len(values) - len(valid),
            "min": min(valid) if valid else None,
            "median": median(valid) if valid else None,
            "max": max(valid) if valid else None}


def factor_summary(items, at):
    from bot.core.apartment_selection_reasons import best_apartment_factors
    available, selected, omitted, count_distribution = (Counter() for _ in range(4))
    eligible, errors = 0, 0
    for item in items:
        try:
            keys = [factor["key"] for factor in best_apartment_factors(item, at)]
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            errors += 1
            continue
        available.update(keys)
        count_distribution[str(len(keys))] += 1
        if "price" in keys and len(keys) >= 4:
            eligible += 1
            selected.update(keys[:4])
            omitted.update(keys[4:])
    return {
        "eligible_four_factor_cards": eligible, "factor_errors": errors,
        "available": {key: available[key] for key in FACTORS},
        "displayed_first_four": {key: selected[key] for key in FACTORS},
        "available_but_outside_first_four": {key: omitted[key] for key in FACTORS},
        "available_factor_count_distribution": dict(sorted(count_distribution.items())),
    }


def audit_snapshot(snapshot, *, now):
    from bot.core.daily_picks import snapshot_status, validate_snapshot
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("items"), list):
        raise ValueError("Snapshot object and items list required")
    generated = timestamp(snapshot["generated_at"])
    items = [item for item in snapshot["items"] if isinstance(item, dict)]
    scopes, identities = Counter(), Counter()
    ages = {key: [] for key in ("observed_age_hours_now", "source_check_age_hours_now")}
    checks = Counter()
    discounts, comparable_counts = [], []
    for item in items:
        comparison = item.get("price_comparison") or {}
        if not isinstance(comparison, dict):
            comparison = {}
        scopes[str(comparison.get("scope") or "missing")] += 1
        identity = item.get("complex_identity")
        identities[str(identity.get("status") or "missing") if isinstance(identity, dict) else "missing"] += 1
        discounts.append(comparison.get("discount_pct"))
        comparable_counts.append(comparison.get("comparable_count"))
        for field, age_key, max_age in (
            ("observed_at", "observed_age_hours_now", timedelta(hours=72)),
            ("source_checked_at", "source_check_age_hours_now", timedelta(hours=2)),
        ):
            try:
                at = timestamp(item.get(field))
                ages[age_key].append((now - at).total_seconds() / 3600)
                if generated - max_age <= at <= generated:
                    checks[field + "_valid_at_publication"] += 1
                if now - max_age <= at <= now:
                    checks[field + "_within_age_window_now"] += 1
            except (ValueError, TypeError, OverflowError):
                checks[field + "_invalid_timestamp"] += 1
        facts = item.get("selection_facts") or {}
        if isinstance(facts, dict):
            checks["owner_claim_true"] += facts.get("is_owner") is True
            checks["owner_claim_and_verified_true"] += (facts.get("is_owner") is True
                                                        and facts.get("owner_source_verified") is True)
        checks["source_status_active"] += item.get("source_status") == "active"
        checks["four_stored_reason_strings"] += (isinstance(item.get("reasons"), list)
                                                   and len(item["reasons"]) == 4
                                                   and all(isinstance(value, str) for value in item["reasons"]))
    validation = {"accepted": False, "retained_count": 0}
    try:
        safe = validate_snapshot(snapshot)
        validation.update(accepted=True, retained_count=len(safe["items"]))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
        validation["error_type"] = type(error).__name__
    policy = snapshot.get("publication_policy")
    policy_fields = ("name", "target_count", "minimum_count", "published_count",
                     "required_strong_factors", "minimum_discount_pct", "reason", "explicit")
    return {
        "as_of": now.isoformat(), "generated_at": generated.isoformat(),
        "selection_date": snapshot.get("selection_date"),
        "snapshot_status": snapshot_status(snapshot, now=now),
        "raw_item_count": len(snapshot["items"]), "object_item_count": len(items),
        "strict_snapshot_validation": validation,
        "publication_policy": {key: policy.get(key) for key in policy_fields} if isinstance(policy, dict) else None,
        "scope_counts": dict(sorted(scopes.items())),
        "identity_status_counts": dict(sorted(identities.items())),
        "discount_pct": numeric_summary(discounts),
        "comparable_count": numeric_summary(comparable_counts),
        "checks": dict(sorted(checks.items())),
        "ages_hours": {key: numeric_summary(values) for key, values in ages.items()},
        "factors_at_publication": factor_summary(items, generated),
        "factors_now": factor_summary(items, now),
        "scope_note": "Saved edition only; not a market-wide candidate audit. Source age at publication and current age are separate checks. No market snapshot or forecast enrichment is read.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--now", help="Optional timezone-aware ISO timestamp")
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    if not (root / "bot/core/daily_picks.py").is_file():
        parser.error("--root must point to the Hatuli repository")
    sys.path.insert(0, str(root))
    now = timestamp(args.now) if args.now else datetime.now(timezone.utc)
    path = args.snapshot or root / "var/daily-picks.json"
    snapshot = json.loads(path.read_text(encoding="utf-8-sig"))
    print(json.dumps(audit_snapshot(snapshot, now=now), ensure_ascii=False, allow_nan=False, indent=2))


if __name__ == "__main__":
    main()
