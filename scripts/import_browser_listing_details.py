"""Fill missing cached fields from a fresh, explicitly observed browser manifest.

Default is PostgreSQL read-only dry-run. --apply only updates photos, floor,
floors_total and year_built that are absent. It never updates asking prices,
price history, source observation dates, identity, contacts or details_fetched.
Optimistic seed checks and row locks prevent overwriting collector updates.
Receipts retain every changed field before/after and support guarded rollback.
No schema initialization, external requests or migrations are performed.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

FIELDS = ("photos", "floor", "floors_total", "year_built")
MAX_AGE = timedelta(hours=2)
PHOTO_HOSTS = frozenset({"astps-photos-kr.kcdn.kz"})
ROW_SQL = """SELECT id,url,price,rooms,area,is_active,is_duplicate,dup_needs_review,
    archived_at,last_seen,photos,floor,floors_total,year_built
    FROM apartment_listings WHERE id=$1"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False)


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encode(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def timestamp(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("missing timestamp")
    if result.tzinfo is None:
        raise ValueError("timestamp must have timezone")
    return result.astimezone(timezone.utc)


def numeric(value, *, positive=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid numeric value")
    if positive and value <= 0:
        raise ValueError("value must be positive")
    return value


def integer_or_none(value, minimum, maximum):
    if value is None:
        return None
    numeric(value)
    if int(value) != value or not minimum <= value <= maximum:
        raise ValueError("integer outside allowed range")
    return int(value)


def source_id(url):
    parsed = urlsplit(url)
    match = re.fullmatch(r"/a/show/(\d{5,20})/?", parsed.path)
    if parsed.scheme != "https" or parsed.netloc not in ("krisha.kz", "www.krisha.kz") or parsed.query or parsed.fragment or not match:
        raise ValueError("not a canonical HTTPS Krisha source URL")
    return match[1]


def photos_value(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError("cached photos are malformed")
    if value is not None and not isinstance(value, list):
        raise ValueError("cached photos are not a list")
    return value


def trusted_photos(value):
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("photo manifest must be a bounded list")
    result = []
    for url in value:
        if not isinstance(url, str) or len(url) > 1024:
            raise ValueError("invalid photo URL")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc not in PHOTO_HOSTS or parsed.fragment
                or not parsed.path.startswith("/") or parsed.path == "/"):
            raise ValueError("photo host is not approved")
        if url not in result:
            result.append(url)
    return result[:40]


def validate_record(record, now):
    lid = str(record.get("listing_id", ""))
    if not re.fullmatch(r"\d{5,20}", lid):
        raise ValueError("invalid listing ID")
    if source_id(record.get("url", "")) != lid or str(record.get("source_listing_id")) != lid:
        raise ValueError("source identity mismatch")
    checked_at = timestamp(record.get("checked_at"))
    if not timedelta(minutes=-2) <= now - checked_at <= MAX_AGE:
        raise ValueError("source observation is expired or in the future")
    if record.get("source_active") is not True:
        raise ValueError("source is not confirmed active")
    before = record.get("before")
    if not isinstance(before, dict):
        raise ValueError("missing optimistic seed")
    timestamp(before.get("last_seen"))
    for field in ("price", "rooms", "area"):
        numeric(before.get(field))
        numeric(record.get("source_" + field))
    # PostgreSQL BIGINT/INTEGER bindings require actual ints, even when a
    # JSON value such as 25000000.0 compares equal to the integer seed.
    before = {**before, "price": integer_or_none(before["price"], 1, 9_000_000_000_000_000),
              "rooms": integer_or_none(before["rooms"], 1, 30)}
    source_price = integer_or_none(record["source_price"], 1, 9_000_000_000_000_000)
    source_rooms = integer_or_none(record["source_rooms"], 1, 30)
    if record["source_price"] != before["price"]:
        return {"status": "source_price_mismatch", "id": lid}
    if record["source_rooms"] != before["rooms"] or abs(record["source_area"] - before["area"]) > .001:
        return {"status": "source_traits_mismatch", "id": lid}
    result = {**record, "listing_id": lid, "before": before, "source_price": source_price,
              "source_rooms": source_rooms, "photos": trusted_photos(record.get("photos", []))}
    result["floor"] = integer_or_none(record.get("floor"), 1, 150)
    result["floors_total"] = integer_or_none(record.get("floors_total"), 1, 150)
    result["year_built"] = integer_or_none(record.get("year_built"), 1800, now.year + 5)
    if result["floor"] and result["floors_total"] and result["floor"] > result["floors_total"]:
        raise ValueError("floor exceeds total floors")
    return result


def plan_row(record: dict, current: dict | None, now: datetime) -> dict:
    """Fail closed on source contradictions and any changed optimistic seed."""
    lid = str(record.get("listing_id", ""))
    try:
        observed = validate_record(record, now)
        if observed.get("status"):
            return {"id": lid, "status": observed["status"], "source_price": record.get("source_price"), "cached_price": record.get("before", {}).get("price")}
        if current is None:
            return {"id": lid, "status": "unknown_listing"}
        if (current.get("url") != record["url"] or current.get("is_active") is not True or current.get("archived_at") is not None
                or current.get("is_duplicate") or current.get("dup_needs_review")):
            return {"id": lid, "status": "current_state_changed"}
        seed = observed["before"]
        if any(current.get(k) != seed.get(k) for k in ("price", "rooms", "area")) or timestamp(current.get("last_seen")) != timestamp(seed.get("last_seen")):
            return {"id": lid, "status": "optimistic_seed_changed"}
        old_photos = photos_value(current.get("photos"))
        if (len(old_photos or []) != seed.get("photo_count")
                or any(current.get(k) != seed.get(k) for k in ("floor", "floors_total", "year_built"))):
            return {"id": lid, "status": "optimistic_seed_changed"}
        updates = {}
        if not old_photos and observed["photos"]:
            updates["photos"] = observed["photos"]
        for field in FIELDS[1:]:
            if current.get(field) is None and observed.get(field) is not None:
                updates[field] = observed[field]
        if any(field in updates for field in ("floor", "floors_total")) and any(
                current.get(field) is not None and observed.get(field) is not None
                and current[field] != observed[field] for field in ("floor", "floors_total")):
            return {"id": lid, "status": "floor_conflict"}
        final_floor = updates.get("floor", current.get("floor"))
        final_total = updates.get("floors_total", current.get("floors_total"))
        if final_floor and final_total and final_floor > final_total:
            return {"id": lid, "status": "floor_conflict"}
        before = {key: (old_photos if key == "photos" else current.get(key)) for key in updates}
        return {"id": lid, "status": "planned" if updates else "no_missing_fields", "url": current["url"],
                "checked_at": record["checked_at"], "seed": {key: seed[key] for key in ("price", "rooms", "area", "last_seen")},
                "before": before, "after": updates}
    except (ValueError, TypeError, KeyError, OverflowError):
        return {"id": lid, "status": "invalid_manifest_record"}


def source_projection(records, now):
    output = []
    for record in records:
        try:
            # Source observation can remain valid even when its price differs
            # from the cached seed. It never alters cache price/last_seen.
            lid = str(record["listing_id"])
            if source_id(record["url"]) != lid or str(record["source_listing_id"]) != lid or record.get("source_active") is not True:
                continue
            if not timedelta(minutes=-2) <= now - timestamp(record["checked_at"]) <= MAX_AGE:
                continue
            for field in ("price", "rooms", "area"):
                numeric(record["source_" + field])
            output.append({key: record[key] for key in ("listing_id", "url", "source_listing_id", "checked_at", "source_active", "source_price", "source_rooms", "source_area")})
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    return {"schema_version": 1, "generated_at": now.isoformat(), "records": output}


async def update_fields(connection, entry, *, rollback=False):
    fields = list(entry["before"] if rollback else entry["after"])
    if not fields or any(field not in FIELDS for field in fields):
        raise ValueError("receipt has unapproved update fields")
    value = entry["before"] if rollback else entry["after"]
    args = []
    sets = []
    for field in fields:
        args.append(json.dumps(value[field], allow_nan=False) if field == "photos" and value[field] is not None else value[field])
        sets.append(f"{field}=${len(args)}" + ("::jsonb" if field == "photos" else ""))
    args.extend([entry["id"], entry["url"], entry["seed"]["price"], entry["seed"]["rooms"], entry["seed"]["area"], timestamp(entry["seed"]["last_seen"])])
    n = len(fields)
    sql = "UPDATE apartment_listings SET " + ",".join(sets) + f""" WHERE id=${n+1} AND url=${n+2}
        AND price IS NOT DISTINCT FROM ${n+3} AND rooms IS NOT DISTINCT FROM ${n+4}
        AND area IS NOT DISTINCT FROM ${n+5} AND last_seen IS NOT DISTINCT FROM ${n+6}
        RETURNING id"""
    identifier = await connection.fetchval(sql, *args)
    if identifier != entry["id"]:
        raise RuntimeError("optimistic update did not affect exactly the selected row")


async def run(args):
    import asyncpg
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file)
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("Set DATABASE_URL explicitly or pass --env-file")
    input_path = Path(args.manifest or args.rollback_manifest).resolve()
    output_path = Path(args.output).resolve()
    if output_path == input_path or (args.source_checks_output and Path(args.source_checks_output).resolve() in (input_path, output_path)):
        raise ValueError("input, receipt and source-checks files must be distinct")
    if args.apply and output_path.exists():
        raise ValueError("Use a new receipt path for each apply operation")
    now = datetime.now(timezone.utc)
    connection = await asyncpg.connect(dsn, command_timeout=60, server_settings={"default_transaction_read_only": "off" if args.apply else "on",
        "lock_timeout": "30000", "statement_timeout": "60000",
        "application_name": "hatuli-browser-details-apply" if args.apply else "hatuli-browser-details-readonly"})
    report = {"schema_version": 1, "created_at": now.isoformat(), "mode": "apply" if args.apply else "dry_run", "rows": []}
    source_records = None
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=not args.apply):
            assert await connection.fetchval("SHOW transaction_read_only") == ("off" if args.apply else "on")
            database = await connection.fetchval("""SELECT current_database() || ':' ||
                COALESCE(inet_server_addr()::text,'local') || ':' ||
                COALESCE(inet_server_port()::text,'local')""")
            report["database_fingerprint"] = hashlib.sha256(database.encode()).hexdigest()
            payload = json.loads(Path(args.manifest or args.rollback_manifest).read_text(encoding="utf-8"))
            if payload.get("schema_version") != 1:
                raise ValueError("unsupported manifest schema")
            if args.rollback_manifest:
                if payload.get("database_fingerprint") != report["database_fingerprint"] or payload.get("mode") != "apply":
                    raise ValueError("rollback receipt does not belong to this database/apply operation")
                report["operation"] = "rollback"
                entries = [entry for entry in payload.get("rows", []) if entry.get("status") == "applied"]
                if len(entries) > 100:
                    raise ValueError("rollback batch too large")
                for entry in entries:
                    current = await connection.fetchrow(ROW_SQL + (" FOR UPDATE" if args.apply else ""), entry["id"])
                    matches = current is not None and all(
                        (photos_value(current[field]) if field == "photos" else current[field]) == value
                        for field, value in entry["after"].items())
                    matches = matches and current["url"] == entry["url"] and all(current[k] == entry["seed"][k] for k in ("price", "rooms", "area")) and timestamp(current["last_seen"]) == timestamp(entry["seed"]["last_seen"])
                    if not matches:
                        report["rows"].append({"id": entry["id"], "status": "rollback_state_changed"})
                        continue
                    if args.apply:
                        await update_fields(connection, entry, rollback=True)
                    report["rows"].append({"id": entry["id"], "status": "rolled_back" if args.apply else "rollback_planned"})
            else:
                source_records = payload.get("records")
                if not isinstance(source_records, list) or not 1 <= len(source_records) <= 100:
                    raise ValueError("manifest must contain 1–100 records")
                ids = [str(record.get("listing_id", "")) for record in source_records]
                if len(set(ids)) != len(ids):
                    raise ValueError("duplicate listing IDs in manifest")
                for record in source_records:
                    current = await connection.fetchrow(ROW_SQL + (" FOR UPDATE" if args.apply else ""), str(record.get("listing_id", "")))
                    entry = plan_row(record, dict(current) if current else None, datetime.now(timezone.utc))
                    if entry["status"] == "planned" and args.apply:
                        await update_fields(connection, entry)
                        entry["status"] = "applied"
                    report["rows"].append(entry)
            report["summary"] = dict(Counter(entry["status"] for entry in report["rows"]))
            report["transaction_status"] = "prepared" if args.apply else "read_only"
            atomic_json(Path(args.output), report)
        if args.apply:
            report["transaction_status"] = "committed"
            atomic_json(Path(args.output), report)
            if source_records is not None and args.source_checks_output:
                atomic_json(Path(args.source_checks_output), source_projection(source_records, now))
        print(encode({"mode": report["mode"], "transaction_status": report["transaction_status"], "summary": report["summary"]}))
    finally:
        await connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--manifest")
    inputs.add_argument("--rollback-manifest")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--env-file")
    parser.add_argument("--output", required=True, help="Audit/rollback receipt JSON path")
    parser.add_argument("--source-checks-output", help="With --apply, atomic JSON of source observations, never DB last_seen")
    asyncio.run(run(parser.parse_args()))
