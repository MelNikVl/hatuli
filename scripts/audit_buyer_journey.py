"""Read-only buyer analysis audit; source and browser verification stay separate.

Selects a reproducible sample of existing Krisha sale listings and exercises
the real buyer analysis, history, identity and alternative-selection functions.
No fixture, migration, import, HTTP request, registration or notification is
allowed. PostgreSQL itself rejects writes, in addition to Python guards.
Output intentionally omits seller/contact details, addresses and coordinates.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_SEED = "hatuli-user-journey-2026-10-13-30"
POPULATION_WHERE = """is_active IS TRUE AND archived_at IS NULL
      AND COALESCE(is_duplicate,FALSE) IS FALSE
      AND COALESCE(dup_needs_review,FALSE) IS FALSE
      AND last_seen BETWEEN now() - interval '48 hours' AND now()
      AND url ~ '^https?://(www\\.)?krisha\\.kz/a/show/[0-9]+/?$'
      AND substring(url from '/a/show/([0-9]+)') = id"""
SELECTION_SQL = """SELECT id,url,price,rooms,area,market_type,is_active,
    first_seen,last_seen,details_fetched,photos,floor,floors_total,year_built
    FROM apartment_listings
    WHERE """ + POPULATION_WHERE + """
    ORDER BY md5(id || $1) LIMIT $2"""


def json_safe(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def photo_count(value) -> int:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return 0
    return len(value) if isinstance(value, list) else 0


def safe_sample(row: dict) -> dict:
    """Whitelist fields rather than removing a growing list of private keys."""
    fields = ("id", "url", "price", "rooms", "area", "market_type", "is_active",
              "first_seen", "last_seen", "details_fetched", "floor", "floors_total", "year_built")
    return {**json_safe({key: row.get(key) for key in fields}),
            "photo_count": photo_count(row.get("photos"))}


def validate_ids(selection: dict) -> list[str]:
    rows = selection.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError("Selection must contain 1–100 rows")
    ids = [str(row.get("id", "")) for row in rows]
    if any(not re.fullmatch(r"\d{5,20}", lid) for lid in ids) or len(ids) != len(set(ids)):
        raise ValueError("Selection IDs must be unique numeric Krisha identifiers")
    return ids


async def forbidden(*_args, **_kwargs):
    raise RuntimeError("Audit is read-only: writes, imports and HTTP requests are disabled")


async def assert_readonly(connection):
    if await connection.fetchval("SHOW default_transaction_read_only") != "on":
        raise RuntimeError("Audit requires default_transaction_read_only=on")
    async with connection.transaction(readonly=True):
        if await connection.fetchval("SHOW transaction_read_only") != "on":
            raise RuntimeError("PostgreSQL transaction is not read-only")


def history_checks(history_rows: list[dict], current_price) -> dict:
    broken = sum(a.get("new_price") != b.get("old_price")
                 for a, b in zip(history_rows, history_rows[1:]))
    return {
        "chain_continuous": broken == 0,
        "chain_breaks": broken,
        "latest_event_matches_current": not history_rows or history_rows[-1].get("new_price") == current_price,
        "timestamps_ordered": all(a.get("changed_at") <= b.get("changed_at")
                                  for a, b in zip(history_rows, history_rows[1:])),
    }


def forecast_summary(value: dict) -> dict:
    value = value or {}
    return {"as_of": value.get("as_of"), "status": value.get("status"),
            "horizons": [{"days": f.get("horizon_days"), "status": f.get("status")}
                         for f in value.get("forecasts", [])]}


async def inspect(row: dict) -> dict:
    from bot.ai_tools.core import resolve_listing
    from bot.core.buyer import analyze_for_buyer
    from bot.core.property_timeline import build_property_timeline
    from bot.buyer.telegram import render_summary, render_alternative, render_history
    from bot.core.buyer_price_chart import render_price_chart
    from bot.db import pg

    lid = str(row["id"])
    result = {"sample": safe_sample(row), "checks": {}, "errors": [],
              "browser": {"status": "pending", "source_url": row["url"]}}
    started = time.monotonic()
    try:
        analysis = await asyncio.wait_for(analyze_for_buyer(lid), timeout=65)
        result["found"] = analysis.get("found")
        if not result["found"]:
            result["message"] = analysis.get("message")
            return result
        detail = analysis["listing"]
        resolved = (await resolve_listing(lid)).data
        result["verdict"] = analysis.get("verdict", {}).get("code")
        result["confidence"] = analysis.get("confidence")
        result["price_summary"] = analysis.get("price_summary")
        result["price_history"] = analysis.get("price_history")
        result["forecast"] = forecast_summary(detail.get("price_forecast"))
        result["identity"] = {key: resolved.get(key) for key in (
            "property_id", "property_link_method", "property_link_confidence",
            "complex_id", "complex_id_resolution")}
        risks = detail.get("risk_analysis") or {}
        result["risk_codes"] = [{"code": r.get("code"), "severity": r.get("severity")}
                                for r in risks.get("items", [])]
        result["unknown_codes"] = [r.get("code") for r in risks.get("unknowns", [])]
        result["mismatch_visible_in_summary"] = any(
            "расхожд" in text.lower() or "отлич" in text.lower()
            for text in analysis.get("negatives", []) + analysis.get("verdict", {}).get("reasons", []))
        alternatives = analysis.get("better_nearby") or []
        result["alternatives"] = [{
            "id": str(a["listing"]["id"]), "url": a["listing"].get("url"),
            "property_id": a["listing"].get("property_id"),
            "active": a["listing"].get("is_active"), "rooms": a["listing"].get("rooms"),
            "area": a["listing"].get("area"), "price": a["listing"].get("price"),
            "distance_m": a.get("distance_m"), "category": a.get("category"),
            "advantages": a.get("advantages"), "tradeoffs": a.get("tradeoffs"),
            "warning_count": len(a.get("warnings", [])),
            "render_under_telegram_limit": len(render_alternative(a, detail)) <= 4096,
        } for a in alternatives]
        alt = result["alternatives"]
        alt_keys = [("property", a["property_id"]) if a.get("property_id") else ("listing", a["id"]) for a in alt]
        checks = result["checks"]
        checks.update({
            "summary_under_telegram_limit": len(render_summary(analysis)) <= 4096,
            "alternative_limit": len(alt) <= 3,
            "alternative_unique_ids": len({a["id"] for a in alt}) == len(alt),
            "alternative_unique_properties": len(set(alt_keys)) == len(alt_keys),
            "alternative_excludes_origin": all(a["id"] != lid for a in alt),
            "alternative_excludes_origin_property": all(not detail.get("property_id") or detail["property_id"] != a.get("property_id") for a in alt),
            "alternative_explicitly_active": all(a["active"] is True for a in alt),
            "alternative_same_rooms": all(a["rooms"] == detail.get("rooms") for a in alt),
            "alternative_in_radius": all(a.get("distance_m", 999999) <= 2000 for a in alt),
            "alternative_area_comparable": all(detail["area"] * .85 <= a["area"] <= detail["area"] * 1.15 for a in alt),
            "alternative_price_comparable": all(detail["price"] * .85 <= a["price"] <= detail["price"] * 1.05 for a in alt),
        })
        history = analysis.get("price_history") or {}
        if history.get("events"):
            checks["history_render_under_telegram_limit"] = len(render_history(analysis, 0)[0]) <= 4096
            checks["price_chart_rendered"] = bool(render_price_chart(history))
        raw_history = [dict(r) for r in await pg.fetch(
            "SELECT old_price,new_price,changed_at FROM price_history WHERE listing_id=$1 ORDER BY changed_at,id", lid)]
        checks.update(history_checks(raw_history, detail.get("price")))
        pid = resolved.get("property_id")
        if pid:
            timeline = await asyncio.wait_for(build_property_timeline(pid), timeout=20)
            result["property_history"] = {"identity": timeline.get("identity"),
                "metrics": timeline.get("metrics"),
                "event_types": dict(Counter(e.get("type") or e.get("event_type") for e in timeline.get("events", [])))}
        result["photo_count"] = len(detail.get("photos") or [])
        result["warnings"] = analysis.get("warnings", [])
    except Exception as exc:
        # Exception strings/tracebacks can contain SQL values or URLs with PII.
        result["errors"].append({"type": type(exc).__name__})
    finally:
        result["analysis_seconds"] = round(time.monotonic() - started, 3)
    return json_safe(result)


def aggregate(rows: list[dict]) -> dict:
    return {"count": len(rows), "found": sum(r.get("found") is True for r in rows),
            "exceptions": sum(bool(r.get("errors")) for r in rows),
            "alternative_counts": dict(Counter(len(r.get("alternatives", [])) for r in rows)),
            "price_changes": sum(r.get("price_history", {}).get("changes", 0) for r in rows),
            "photo_missing": sum(r.get("photo_count", 0) == 0 for r in rows),
            "failed_checks": [{"id": r["sample"]["id"], "checks": [k for k, v in r.get("checks", {}).items() if v is False]}
                              for r in rows if any(v is False for v in r.get("checks", {}).values())]}


async def main(args):
    import asyncpg
    import httpx
    from bot.db import pg
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file)
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("Set DATABASE_URL explicitly or pass --env-file")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    # Never call pg.init_pool: it applies production schema migrations.
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6, command_timeout=30,
        server_settings={"default_transaction_read_only": "on", "application_name": "hatuli-buyer-journey-readonly"},
        init=assert_readonly)
    pg._pool = pool
    try:
        if args.selection:
            selection = json.loads(Path(args.selection).read_text(encoding="utf-8"))
            ids = validate_ids(selection)
            found_rows = await pg.fetch("SELECT id,url,price,rooms,area,market_type,is_active,first_seen,last_seen,details_fetched,photos,floor,floors_total,year_built FROM apartment_listings WHERE id=ANY($1::text[])", ids)
            indexed = {str(r["id"]): dict(r) for r in found_rows}
            if any(lid not in indexed for lid in ids):
                raise ValueError("Selected ID no longer exists; audit will not import it")
            rows = [indexed[lid] for lid in ids]
            selection = {"seed": selection.get("seed"), "population": selection.get("population"), "created_at": datetime.now(timezone.utc).isoformat(),
                         "rows": [safe_sample(r) for r in rows]}
        else:
            rows = [dict(r) for r in await pg.fetch(SELECTION_SQL, args.seed, args.limit)]
            population = await pg.fetchval("SELECT count(*) FROM apartment_listings WHERE " + POPULATION_WHERE)
            selection = {"seed": args.seed, "population": population,
                         "population_definition": "active, archived_at NULL, no duplicate/review flag, seen in past 48 hours, strict source URL matching ID",
                         "created_at": datetime.now(timezone.utc).isoformat(),
                         "rows": [safe_sample(r) for r in rows]}
        (output / "selection.json").write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.select_only:
            print(json.dumps({"count": len(rows), "ids": [r["id"] for r in rows]}, ensure_ascii=False), flush=True)
            return
        with ExitStack() as stack:
            for target in ("bot.db.pg.execute", "bot.db.pg.init_pool", "bot.db.pg._apply_migrations", "bot.core.buyer.import_listing",
                           "httpx.AsyncClient.send", "httpx.Client.send"):
                stack.enter_context(patch(target, forbidden))
            results = []
            for row in rows:
                value = await inspect(row)
                results.append(value)
                (output / f"{row['id']}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"id": row["id"], "found": value.get("found"), "seconds": value.get("analysis_seconds"),
                                  "alternative_count": len(value.get("alternatives", [])), "errors": value["errors"]}), flush=True)
        report = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                  "guards": {"postgres_default_readonly": True, "migration_calls_disabled": True,
                             "writes_disabled": True, "http_requests_disabled": True, "imports_disabled": True},
                  "coverage": "backend-existing-listings-only; browser and source checks are separate", "summary": aggregate(results), "rows": results}
        (output / "backend-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    finally:
        pg._pool = None
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--selection", help="JSON file with rows[].id from a previous selection")
    parser.add_argument("--select-only", action="store_true")
    parser.add_argument("--env-file", help="Explicit .env path; credentials are never printed")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be in 1–100")
    asyncio.run(main(args))
