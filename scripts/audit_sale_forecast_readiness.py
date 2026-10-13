#!/usr/bin/env python3
"""Aggregate-only sale forecasting audit; never trains or writes to PostgreSQL.

Archives, disappearance predictions, and developer stock marked sold are not
treated as verified apartment transactions. Only this offline audit reads DB;
public requests must continue to read validated local snapshots.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path


QUERIES = {
    "outcome_tables": """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema='public' AND
              (table_name LIKE '%sale%' OR table_name LIKE '%transaction%'
               OR table_name LIKE '%closing%' OR table_name='outcome_labels')
        ORDER BY table_name
    """,
    "sale_labels": """
        SELECT label,source,count(*) AS rows,min(labeled_at) AS first_at,
               max(labeled_at) AS last_at
        FROM sale_outcome_labels GROUP BY label,source ORDER BY label,source
    """,
    "transaction_fields": """
        SELECT table_name,column_name,data_type FROM information_schema.columns
        WHERE table_schema='public'
          AND (table_name LIKE '%sale%' OR table_name LIKE '%transaction%'
               OR table_name IN ('apartment_listings','outcome_labels','newbuild_units'))
          AND (column_name LIKE '%sold%' OR column_name LIKE '%sale%'
               OR column_name LIKE '%transaction%' OR column_name LIKE '%closing%')
        ORDER BY table_name,ordinal_position
    """,
    "archived_listings": """
        SELECT archive_reason,count(*) AS listings,min(archived_at) AS first_at,
               max(archived_at) AS last_at
        FROM apartment_listings WHERE archived_at IS NOT NULL
        GROUP BY archive_reason ORDER BY archive_reason
    """,
    "liquidity": """
        SELECT count(*) AS rows,count(DISTINCT listing_id) AS listings,
               min(as_of) AS first_day,max(as_of) AS last_day,
               count(*) FILTER(WHERE as_of>=current_date-3) AS fresh_rows,
               count(DISTINCT listing_id) FILTER(WHERE as_of>=current_date-3) AS fresh_listings
        FROM liquidity_predictions
    """,
    "newbuild_sold_stock": """
        SELECT source,status,count(*) AS units,
               count(*) FILTER(WHERE sold_at IS NOT NULL) AS has_sold_at,
               min(sold_at) AS first_sold_at,max(sold_at) AS last_sold_at
        FROM newbuild_units WHERE status='sold'
        GROUP BY source,status ORDER BY source,status
    """,
}

DAILY_LIQUIDITY_SQL = """
    WITH latest AS (
        SELECT DISTINCT ON (listing_id) listing_id,as_of,p_exit14,p_cut30
        FROM liquidity_predictions WHERE listing_id=ANY($1::text[])
        ORDER BY listing_id,as_of DESC
    )
    SELECT count(*) AS matched_listings,
           count(*) FILTER(WHERE as_of>=current_date-3) AS fresh_listings,
           count(*) FILTER(WHERE as_of>=current_date-3 AND p_exit14 IS NOT NULL) AS fresh_exit_predictions,
           count(*) FILTER(WHERE as_of>=current_date-3 AND p_cut30 IS NOT NULL) AS fresh_cut_predictions,
           min(as_of) AS first_day,max(as_of) AS last_day
    FROM latest
"""


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def public_artifact_audit(root: Path) -> dict:
    """Allowlist audit metadata; exclude offers, feature values, seller/address data."""
    model = read_json(root / "var/price-forecast-model.json")
    public = read_json(root / "var/price-forecasts.json")
    horizons = model.get("horizons", [])
    if not isinstance(horizons, list):
        horizons = []
    forecast = {
        "target": model.get("target"), "target_label": model.get("target_label"),
        "generated_at": public.get("generated_at"),
        "source_history_verified": public.get("source_history_verified"),
        "audit": {key: model.get("audit", {}).get(key) for key in (
            "input_listings", "input_points", "insufficient_source_points",
            "missing_attribute_history", "unconfirmed_source_points", "usable_listings", "usable_points")},
        "horizons": [{"days": row.get("horizon_days"), "status": row.get("status"),
                      "validation": row.get("validation", {}), "fold_count": len(row.get("folds", []))}
                     for row in horizons if isinstance(row, dict)],
        "published_offers": len(public.get("offers", {})) if isinstance(public.get("offers"), dict) else 0,
    }
    liquidity = {}
    for target in ("exit14", "cut30"):
        version = read_json(root / f"models/liquidity/{target}_active.json").get("version")
        # A pointer may not turn the metadata read into an arbitrary file lookup.
        if not isinstance(version, str) or len(version) != 15 or not version.replace("T", "").isdigit():
            continue
        meta = read_json(root / f"models/liquidity/{target}_{version}.json")
        liquidity[target] = {
            "version": version, "trained_at": meta.get("trained_at"),
            "metrics": {key: meta.get("metrics", {}).get(key) for key in (
                "target", "n", "pos_rate", "t0s", "auc_cv",
                "auc_baseline_price_score", "auc_diff_ci95", "gate_passed", "auc_cv_by_t0")},
        }
    return {"asking_price_forecast": forecast, "liquidity_models": liquidity}


async def audit(dsn: str, root: Path) -> dict:
    import asyncpg

    conn = await asyncpg.connect(
        dsn, command_timeout=60,
        server_settings={"default_transaction_read_only": "on", "statement_timeout": "60000",
                         "application_name": "hatuli_sale_forecast_readiness_readonly"},
    )
    try:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            read_only = await conn.fetchval("SHOW transaction_read_only")
            if read_only != "on":
                raise RuntimeError("Sale forecasting audit requires a read-only transaction")
            report = {"database_time": await conn.fetchval("SELECT now()"),
                      "transaction_read_only": read_only, "database_writes": 0}
            for name, sql in QUERIES.items():
                report[name] = [dict(row) for row in await conn.fetch(sql)]
            items = read_json(root / "var/daily-picks.json").get("items", [])
            identifiers = [item["id"] for item in items
                           if isinstance(item, dict) and isinstance(item.get("id"), str)]
            report["daily_selection"] = {
                "selected_listings": len(identifiers),
                **dict(await conn.fetchrow(DAILY_LIQUIDITY_SQL, identifiers)),
            }
            manual = {row["label"]: row["rows"] for row in report["sale_labels"] if row["source"] == "manual"}
            report["manual_outcome_progress"] = {
                "sold": manual.get("sold", 0), "withdrawn": manual.get("withdrawn", 0),
                "sold_plus_withdrawn": manual.get("sold", 0) + manual.get("withdrawn", 0),
                "minimum_to_start_sale_probability_research": 300,
                "closing_price_fields_found": [row for row in report["transaction_fields"]
                    if row["table_name"] != "newbuild_units" and "price" in row["column_name"]],
                "closing_date_fields_found": [row for row in report["transaction_fields"]
                    if row["table_name"] != "newbuild_units"
                    and (row["column_name"].endswith("_at") or row["column_name"].endswith("_date"))],
            }
            report.update(public_artifact_audit(root))
            return report
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from dotenv import load_dotenv

    load_dotenv(args.root / ".env")
    report = asyncio.run(audit(os.environ["DATABASE_URL"], args.root))
    result = json.dumps(report, ensure_ascii=False, default=str, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result, encoding="utf-8")
    else:
        print(result, end="")


if __name__ == "__main__":
    main()
