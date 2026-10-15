#!/usr/bin/env python3
"""Private, bounded rental stage-0 audit and reviewable maintenance plans.

Dry run is the default and uses a raw read-only connection. No application
pool, migrations, source requests, index rebuild or Deal Score calculation.
Examples (the environment file must be supplied explicitly):
  python scripts/rental_stage0_maintenance.py audit --env-file /private/env --output /private/audit.json
  python scripts/rental_stage0_maintenance.py backfill --env-file /private/env --output /private/plan.json --limit 500
  python scripts/rental_stage0_maintenance.py backfill --env-file /private/env --apply --plan /private/plan.json --expected-plan-sha HASH --output /private/applied.json
Process further batches with the returned next_after_id. Do not reuse an
applied plan: changed source rows, settings or evidence invalidate it.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VERSION = 1
MAX_ROWS = 1000
MAX_PLAN_BYTES = 8_000_000
LOCK_NAME = "hatuli:rental-stage0-maintenance:v1"
RENTAL_FIELDS = ("id", "title", "price", "area", "rooms", "city", "prop_type", "complex_name", "complex_url",
                 "address", "lat", "lon", "rent_period", "rent_period_conflict", "complex_id", "hex_id", "hex_edge_m",
                 "is_active", "archived_at", "is_duplicate", "dup_needs_review", "duplicate_of", "last_seen")
SALE_FIELDS = ("id", "price", "area", "rooms", "city", "district", "complex_name", "complex_id",
               "complex_resolution", "resolved_house_id", "house_attribution", "is_active", "archived_at",
               "is_duplicate", "dup_needs_review", "duplicate_of", "last_seen", "est_rent", "yield_pct",
               "net_yield_pct", "payback_years", "rent_source")
WRITABLE = {
    "backfill": ("rental_listings", RENTAL_FIELDS, ("rent_period", "rent_period_conflict", "complex_id", "hex_id", "hex_edge_m")),
    "recompute": ("apartment_listings", SALE_FIELDS, ("est_rent", "yield_pct", "net_yield_pct", "payback_years", "rent_source")),
}
SETTINGS_KEYS = ("HEX_EDGE_M", "NET_YIELD_VACANCY", "NET_YIELD_OPEX_SHARE", "NET_YIELD_PURCHASE_COST",
                 "RENT_MIN_SAMPLE_COMPLEX", "RENT_MIN_SAMPLE_AREA")
REQUIRED = {
    "rental_listings": set(RENTAL_FIELDS), "apartment_listings": set(SALE_FIELDS),
    "rental_index": {"rent_period", "complex_id", "furnishing", "window_days", "source_oldest_at",
                     "source_newest_at", "policy_version", "active_supply", "updated_at", "sample_count"},
}


class MaintenanceError(RuntimeError):
    """A user-safe refusal, without a database URL or private source contents."""


def json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def digest(value):
    encoded = json.dumps(json_value(value), sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def seal(plan):
    return {**plan, "plan_sha256": digest({key: value for key, value in plan.items() if key != "plan_sha256"})}


def verify_plan(plan, expected_sha, operation):
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise MaintenanceError("A complete expected plan SHA-256 is required")
    if not isinstance(plan, dict) or plan.get("version") != VERSION or plan.get("operation") != operation:
        raise MaintenanceError("Plan version or operation does not match")
    actual = seal(plan)["plan_sha256"]
    if not hmac.compare_digest(actual, expected_sha) or plan.get("plan_sha256") != expected_sha:
        raise MaintenanceError("Plan hash does not match; regenerate and review the plan")
    changes = plan.get("changes")
    if not isinstance(changes, list) or len(changes) > MAX_ROWS or operation not in WRITABLE:
        raise MaintenanceError("Invalid or unbounded maintenance plan")
    identifiers = set()
    allowed = set(WRITABLE[operation][2])
    for item in changes:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
                or item["id"] in identifiers or not isinstance(item.get("after"), dict)
                or set(item["after"]) != allowed or not isinstance(item.get("row_sha256"), str)):
            raise MaintenanceError("Invalid plan row or columns")
        identifiers.add(item["id"])


def private_json(path, value):
    """Atomic private artifact; never follow a supplied symlink or publish in static."""
    path = Path(path)
    validate_output(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = json.dumps(json_value(value), ensure_ascii=False, allow_nan=False, indent=2)+"\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".rental-stage0-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def validate_output(path):
    path = Path(path)
    if (path.is_symlink() or "static" in {part.lower() for part in path.resolve().parts}
            or path.exists() and not path.is_file()):
        raise MaintenanceError("A private output destination is required")


def load_plan(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > MAX_PLAN_BYTES:
        raise MaintenanceError("Invalid plan file")
    return json.loads(path.read_text(encoding="utf-8"))


def load_dsn(path):
    from dotenv import dotenv_values
    values = dotenv_values(Path(path), interpolate=False)
    dsn = values.get("DATABASE_URL")
    if not isinstance(dsn, str) or not dsn.startswith(("postgresql://", "postgres://")):
        raise MaintenanceError("The explicit environment file must contain DATABASE_URL")
    return dsn


@contextmanager
def read_adapter(connection, values):
    """Reuse existing read-only resolvers/lookup without an application pool.

    This is a standalone sequential CLI process. Every helper gets the same
    snapshot connection; any accidental writer or pool initializer fails.
    """
    from unittest.mock import patch
    from bot.db import pg, settings
    async def read(method, sql, *args):
        if (not sql.lstrip().upper().startswith(("SELECT", "WITH"))
                or re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|COPY|CALL|DO|GRANT|REVOKE)\b", sql, re.I)):
            raise MaintenanceError("A helper attempted a non-read query")
        return await getattr(connection, method)(sql, *args)
    async def fetch(sql, *args):
        return await read("fetch", sql, *args)
    async def fetchrow(sql, *args):
        return await read("fetchrow", sql, *args)
    async def fetchval(sql, *args):
        return await read("fetchval", sql, *args)
    def deny(*args, **kwargs):
        raise MaintenanceError("Application writes, pools and migrations are disabled")
    def get(key, default=None):
        return values.get(key, default if default is not None else settings.DEFAULTS.get(key, ""))
    with patch.object(pg, "fetch", fetch), patch.object(pg, "fetchrow", fetchrow), patch.object(pg, "fetchval", fetchval), \
         patch.object(pg, "execute", deny), patch.object(pg, "get_pool", deny), patch.object(pg, "init_pool", deny), \
         patch.object(settings, "get", get):
        yield


def row_hash(row, fields):
    return digest({key: row.get(key) for key in fields})


async def backfill_proposal(row, values):
    from bot.core.rental_period import classify_rental_period, rental_period_conflict
    from bot.core.rental_stage0_identity import resolve_rental_complex, observed_hex
    previous = row.get("rent_period") or "unknown"
    explicit = classify_rental_period(title=row.get("title"))
    conflict = (row.get("rent_period_conflict") is True or rental_period_conflict(title=row.get("title"))
                or previous in ("daily", "monthly") and explicit in ("daily", "monthly") and previous != explicit)
    period = "unknown" if conflict else explicit if explicit != "unknown" else previous
    if period not in ("monthly", "daily", "unknown"):
        period = "unknown"
    cid = await resolve_rental_complex(complex_url=row.get("complex_url"), complex_name=row.get("complex_name"),
                                       address=row.get("address"), title=row.get("title"))
    try:
        edge = float(values.get("HEX_EDGE_M", "50"))
    except (ValueError, TypeError, OverflowError):
        raise MaintenanceError("Invalid HEX_EDGE_M setting") from None
    if not math.isfinite(edge) or not 25 <= edge <= 500:
        raise MaintenanceError("Invalid HEX_EDGE_M setting")
    geo = observed_hex(row.get("lat"), row.get("lon"), edge)
    after = {"rent_period": period, "rent_period_conflict": bool(conflict), "complex_id": cid,
             "hex_id": geo["hex_id"] if geo else None, "hex_edge_m": geo["hex_edge_m"] if geo else None}
    evidence = {"period_evidence": "explicit_title" if explicit != "unknown" else "no_new_explicit_period",
                "observed_period": explicit, "sticky_conflict": bool(conflict), "resolved_complex_id": cid,
                "hex_basis": "stored_observed_coordinates" if geo else "no_valid_observed_coordinates", "edge_m": edge}
    return after, evidence


async def recompute_proposal(row, values, now):
    from bot.core.rental_parser import lookup_rental_estimate
    from bot.core.rental_yield import NetYieldParams, monthly_yield_scenario
    empty = dict.fromkeys(WRITABLE["recompute"][2])
    observed = row.get("last_seen")
    if isinstance(observed, str):
        observed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    if (row.get("is_active") is not True or row.get("archived_at") is not None
            or row.get("is_duplicate") is True or row.get("dup_needs_review") is True or row.get("duplicate_of")
            or row.get("city") != "astana" or not isinstance(observed, datetime) or observed.tzinfo is None
            or not now-timedelta(hours=72) <= observed <= now):
        return empty, {"reason": "sale_not_fresh_clean_active"}
    params = NetYieldParams.from_settings(lambda key, default: values.get(key, default))
    cid, name = row.get("complex_id"), row.get("complex_name")
    if row.get("complex_resolution") in ("geo", "unbound") or row.get("house_attribution") in ("geo", "address_geo"):
        cid, name = None, None
    estimate = await lookup_rental_estimate(row["city"], row.get("district"), name, row.get("rooms"), complex_id=cid)
    if not estimate:
        return empty, {"reason": "no_fresh_policy3_index", "params_used": asdict(params)}
    area = row.get("area")
    rent = (round(estimate["price_per_sqm"]*area) if estimate.get("price_per_sqm") and type(area) in (int, float)
            and math.isfinite(area) and area > 0 else estimate["median_price"])
    scenario = monthly_yield_scenario(row.get("price"), rent, params)
    if not scenario:
        return empty, {"reason": "invalid_price_or_rent", "params_used": asdict(params)}
    after = {"est_rent": rent, "yield_pct": scenario["gross_yield_pct"], "net_yield_pct": scenario["net_yield_pct"],
             "payback_years": scenario["gross_payback_years"], "rent_source": f"{estimate['level']}, n={estimate['sample_count']}"}
    return after, {"asking_prices": True, "scenario": True, "params_used": asdict(params), "index_source": json_value(estimate)}


async def schema_status(connection):
    rows = await connection.fetch("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=ANY($1::text[])", list(REQUIRED))
    available = {table: set() for table in REQUIRED}
    for row in rows:
        available[row["table_name"]].add(row["column_name"])
    missing = {table: sorted(fields-available[table]) for table, fields in REQUIRED.items() if fields-available[table]}
    return {"ready": not missing, "missing_columns": missing}


async def aggregate_report(connection, ready):
    report = {"rentals": json_value([dict(row) for row in await connection.fetch("""
        SELECT coalesce(prop_type,'unknown') AS prop_type,count(*) AS total,
          count(*) FILTER(WHERE is_active IS TRUE) AS active,
          count(*) FILTER(WHERE is_active IS TRUE AND last_seen>=now()-interval '72 hours') AS seen_72h,
          percentile_cont(ARRAY[.25,.5,.75]) WITHIN GROUP(ORDER BY price/nullif(area,0))
            FILTER(WHERE price>0 AND area>0) AS price_per_m2_diagnostic_only
        FROM rental_listings GROUP BY 1 ORDER BY 1
    """)])}
    if ready:
        report["periods"] = [dict(row) for row in await connection.fetch("""
            SELECT rent_period,rent_period_conflict,count(*) AS total,
              count(*) FILTER(WHERE complex_id IS NOT NULL) AS with_complex_id,
              count(*) FILTER(WHERE hex_id IS NOT NULL AND hex_edge_m IS NOT NULL) AS with_hex
            FROM rental_listings GROUP BY 1,2 ORDER BY 1,2
        """)]
        report["sales"] = [dict(row) for row in await connection.fetch("""
            SELECT coalesce(split_part(rent_source,',',1),'unknown') AS source,count(*) AS total,
              count(*) FILTER(WHERE est_rent>0) AS with_rent,
              percentile_cont(ARRAY[.25,.5,.75]) WITHIN GROUP(ORDER BY net_yield_pct) AS net_yield_distribution
            FROM apartment_listings WHERE city='astana' AND is_active IS TRUE
              AND NOT coalesce(is_duplicate,false) AND NOT coalesce(dup_needs_review,false)
              AND nullif(btrim(duplicate_of),'') IS NULL AND archived_at IS NULL
            GROUP BY 1 ORDER BY 1
        """)]
    return json_value(report)


async def settings_snapshot(connection):
    rows = await connection.fetch("SELECT key,value FROM app_settings WHERE key=ANY($1::text[]) ORDER BY key", list(SETTINGS_KEYS))
    return {row["key"]: row["value"] for row in rows}


async def database_identity(connection):
    row = await connection.fetchrow("SELECT current_database() AS database,current_schema() AS schema,inet_server_addr()::text AS server,inet_server_port() AS port")
    return digest(dict(row))


def change_record(operation, row, after, evidence):
    fields, allowed = WRITABLE[operation][1:]
    def same(left, right):
        if (type(left) in (Decimal, int, float) and type(right) in (Decimal, int, float)):
            return Decimal(str(left)) == Decimal(str(right))
        return left == right
    if all(same(row.get(key), after[key]) for key in allowed):
        return None
    return {"id": row["id"], "before": json_value({key: row.get(key) for key in allowed}), "after": after,
            "row_sha256": row_hash(row, fields), "evidence": evidence, "evidence_sha256": digest(evidence)}


async def propose(operation, row, values, now):
    return (await backfill_proposal(row, values) if operation == "backfill"
            else await recompute_proposal(row, values, now))


async def build_plan(connection, operation, *, limit=500, after_id=""):
    if type(limit) is not int or not 1 <= limit <= MAX_ROWS:
        raise MaintenanceError("Limit must be between 1 and 1000")
    status = await schema_status(connection)
    before = await aggregate_report(connection, status["ready"])
    now = await connection.fetchval("SELECT clock_timestamp()")
    plan = {"version": VERSION, "operation": operation, "generated_at": now.isoformat(), "dry_run": True,
            "schema": status, "before": before, "database_writes": 0, "index_rebuild_performed": False,
            "removed_index_rows": None, "deal_score_recompute_performed": False, "changes": [],
            "database_identity_sha256": await database_identity(connection)}
    if operation == "audit":
        return seal(plan)
    if not status["ready"]:
        raise MaintenanceError("Stage-0 schema is incomplete; audit first and apply the reviewed migrations separately")
    table, fields, _ = WRITABLE[operation]
    # Names are constants selected above. User values are bound parameters.
    scope = "city='astana'" if operation == "backfill" else "city='astana' AND is_active IS TRUE AND archived_at IS NULL AND NOT coalesce(is_duplicate,false) AND NOT coalesce(dup_needs_review,false) AND nullif(btrim(duplicate_of),'') IS NULL"
    rows = await connection.fetch(f"SELECT {','.join(fields)} FROM {table} WHERE {scope} AND id>$1 ORDER BY id LIMIT $2", after_id, limit+1)
    values = await settings_snapshot(connection)
    plan.update(settings_sha256=digest(values), scanned=min(len(rows), limit), limit=limit, after_id=after_id,
                has_more=len(rows)>limit, next_after_id=rows[min(len(rows), limit)-1]["id"] if rows else after_id)
    with read_adapter(connection, values):
        for source in rows[:limit]:
            row = dict(source)
            after, evidence = await propose(operation, row, values, now)
            item = change_record(operation, row, after, evidence)
            if item:
                plan["changes"].append(item)
    return seal(plan)


async def apply_plan(connection, plan, expected_sha, operation):
    verify_plan(plan, expected_sha, operation)
    if not (await schema_status(connection))["ready"]:
        raise MaintenanceError("Required stage-0 columns are missing")
    await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", LOCK_NAME)
    if await database_identity(connection) != plan.get("database_identity_sha256"):
        raise MaintenanceError("The plan belongs to a different database destination")
    values = await settings_snapshot(connection)
    if digest(values) != plan.get("settings_sha256"):
        raise MaintenanceError("Settings changed after planning; regenerate the plan")
    now = await connection.fetchval("SELECT clock_timestamp()")
    before = await aggregate_report(connection, True)
    table, fields, allowed = WRITABLE[operation]
    updates = []
    with read_adapter(connection, values):
        for item in sorted(plan["changes"], key=lambda value: value["id"]):
            current = await connection.fetchrow(f"SELECT {','.join(fields)} FROM {table} WHERE id=$1 FOR UPDATE", item["id"])
            if current is None or row_hash(dict(current), fields) != item["row_sha256"]:
                raise MaintenanceError("A source row changed after planning; no batch changes were applied")
            after, evidence = await propose(operation, dict(current), values, now)
            if digest(after) != digest(item["after"]) or digest(evidence) != item["evidence_sha256"]:
                raise MaintenanceError("Resolution or index evidence changed; regenerate the plan")
            updates.append((item["id"], after))
    # All rows/evidence must pass before the first write. Caller owns one bounded transaction.
    assignments = ",".join(f"{field}=${index}" for index, field in enumerate(allowed, 2))
    for identifier, after in updates:
        values = [after[field] for field in allowed]
        if operation == "recompute":
            values[1] = Decimal(str(values[1])) if values[1] is not None else None
            values[3] = Decimal(str(values[3])) if values[3] is not None else None
        result = await connection.execute(f"UPDATE {table} SET {assignments} WHERE id=$1", identifier, *values)
        if result != "UPDATE 1":
            raise MaintenanceError("An expected row was not updated; transaction must roll back")
    return {"version": VERSION, "operation": operation, "dry_run": False, "plan_sha256": expected_sha,
            "applied_at": now.isoformat(), "database_writes": len(updates), "before": before,
            "after": await aggregate_report(connection, True), "removed_index_rows": None,
            "index_rebuild_performed": False, "deal_score_recompute_performed": False,
            "deal_score_recompute_required": operation == "recompute" and bool(updates),
            "next_step": "Rebuild the monthly index separately after backfill; generate a new recompute plan afterwards. Run the reviewed Deal Score job after applying recomputed sale estimates."}


async def run(args, *, connect=None):
    if args.apply and (not args.plan or not args.expected_plan_sha or args.operation == "audit"):
        raise MaintenanceError("Apply requires --plan, --expected-plan-sha and a maintenance operation")
    if not args.apply and (args.plan or args.expected_plan_sha):
        raise MaintenanceError("Plan input and expected SHA are only accepted with --apply")
    if args.plan and Path(args.plan).resolve() == Path(args.output).resolve():
        raise MaintenanceError("Keep the reviewed plan and application report in different files")
    if Path(args.env_file).resolve() == Path(args.output).resolve():
        raise MaintenanceError("Output must not overwrite the explicit environment file")
    validate_output(args.output)
    plan = load_plan(args.plan) if args.apply else None
    if args.apply:
        verify_plan(plan, args.expected_plan_sha, args.operation)
    if connect is None:
        import asyncpg
        connect = asyncpg.connect
    connection = await connect(load_dsn(args.env_file), command_timeout=40, server_settings={
        "application_name": "hatuli-rental-stage0-maintenance", "default_transaction_read_only": "off" if args.apply else "on",
        "statement_timeout": "30000", "lock_timeout": "5000"})
    try:
        async with connection.transaction(readonly=not args.apply, isolation="repeatable_read"):
            if await connection.fetchval("SHOW transaction_read_only") != ("off" if args.apply else "on"):
                raise MaintenanceError("Connection read-only state did not match the requested mode")
            result = (await apply_plan(connection, plan, args.expected_plan_sha, args.operation) if args.apply
                      else await build_plan(connection, args.operation, limit=args.limit, after_id=args.after_id))
        try:
            private_json(args.output, result)
        except Exception:
            if args.apply:
                raise MaintenanceError("The database batch committed, but its private report could not be saved; audit again before retrying") from None
            raise
        return result
    finally:
        await connection.close()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("operation", choices=("audit", "backfill", "recompute"))
    result.add_argument("--env-file", required=True, type=Path)
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--limit", type=int, default=500)
    result.add_argument("--after-id", default="")
    result.add_argument("--apply", action="store_true")
    result.add_argument("--plan", type=Path)
    result.add_argument("--expected-plan-sha")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = asyncio.run(run(args))
    except MaintenanceError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Database exceptions can carry DSNs/source contents. Print only the class.
        print(f"Maintenance failed ({type(exc).__name__}); no private error contents were printed", file=sys.stderr)
        return 1
    print(json.dumps({key: result.get(key) for key in ("operation", "dry_run", "plan_sha256", "scanned", "database_writes")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
