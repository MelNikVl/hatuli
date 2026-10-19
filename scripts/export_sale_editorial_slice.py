#!/usr/bin/env python3
"""Export a private, bounded sale identity audit. PostgreSQL is read-only.

No init_pool, migration, model refresh, source request or public publication.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.core.complex_membership import active_sale_membership_ctes_sql
from bot.core.daily_pick_identity import annotate_daily_pick_identity_rows
from bot.core.sale_editorial_slice import MAX_ROWS, KNOWN_SOURCE_CONFLICTS, build_sale_editorial_slice
from scripts.publish_daily_picks import IDENTITY_READS

SALES_SQL = """
SELECT a.id,a.url,a.price::float8 AS price,a.area::float8 AS area,a.rooms,a.floor,a.floors_total,a.district,a.address,
       a.market_type,a.last_seen,a.is_active,a.archived_at,a.is_duplicate,a.dup_needs_review,a.duplicate_of,
       a.complex_id,a.complex_resolution,a.resolved_house_id,a.house_attribution,a.finish_level,
       complex_name_key(a.complex_name) AS identity_raw_name_key,
       a.complex_name AS stored_source_complex_name,
       pl.property_id, p.identity_status AS property_identity_status,
       coalesce(pl.property_link_count,0) AS property_link_count
FROM apartment_listings a
LEFT JOIN LATERAL (
    SELECT min(property_id) AS property_id,count(DISTINCT property_id) AS property_link_count
    FROM property_listings WHERE listing_id=a.id
) pl ON TRUE
LEFT JOIN properties p ON p.property_id=pl.property_id
WHERE a.is_active IS TRUE AND a.last_seen BETWEEN $1::timestamptz-interval '72 hours' AND $1
ORDER BY a.id LIMIT $2
"""
MEMBERSHIP_SQL = active_sale_membership_ctes_sql() + """
SELECT m.listing_id,m.complex_id FROM listing_membership m
JOIN apartment_listings a ON a.id=m.listing_id
WHERE a.is_active IS TRUE AND a.last_seen BETWEEN $1::timestamptz-interval '72 hours' AND $1
ORDER BY m.listing_id,m.complex_id LIMIT $2
"""


async def read_sale_slice(dsn, now, *, connect=None):
    if connect is None:
        import asyncpg
        connect = asyncpg.connect
    connection = await connect(dsn, command_timeout=60,
        server_settings={"default_transaction_read_only": "on", "application_name": "hatuli_sale_editorial_readonly"})
    try:
        async with connection.transaction(readonly=True, isolation="repeatable_read"):
            if await connection.fetchval("SHOW transaction_read_only") != "on":
                raise ValueError("Read-only transaction required")
            if await connection.fetchval("SHOW transaction_isolation") != "repeatable read":
                raise ValueError("Repeatable read required")
            rows = [dict(r) for r in await connection.fetch(SALES_SQL, now, MAX_ROWS+1)]
            if len(rows) > MAX_ROWS:
                raise ValueError("Incomplete bounded sale scan")
            evidence = {}
            for name, sql, bound in IDENTITY_READS:
                if name=='complexes':
                    if 'SELECT id,name,' not in sql:
                        raise ValueError('Catalog query changed; review address projection')
                    sql=sql.replace('SELECT id,name,','SELECT id,name,address,',1)
                values = [dict(r) for r in await connection.fetch(sql, bound+1)]
                if len(values) > bound:
                    raise ValueError("Incomplete bounded identity evidence: " + name)
                evidence[name] = values
            memberships = [dict(r) for r in await connection.fetch(MEMBERSHIP_SQL, now, MAX_ROWS+1)]
            if len(memberships) > MAX_ROWS:
                raise ValueError("Incomplete bounded membership map")
            mapping = {}
            for row in memberships:
                if row["listing_id"] in mapping and mapping[row["listing_id"]] != row["complex_id"]:
                    raise ValueError("Ambiguous canonical membership result")
                mapping[row["listing_id"]] = row["complex_id"]
            for row in rows:
                row["membership_complex_id"] = mapping.get(row["id"])
            # Keep a hold on relists even when the original conflicting source
            # has left today's active window. This does not change its link.
            holds = await connection.fetch(
                "SELECT property_id FROM property_listings WHERE listing_id=ANY($1::text[])",
                [r['id'] for r in KNOWN_SOURCE_CONFLICTS])
            annotated=annotate_daily_pick_identity_rows(rows, **evidence)
            catalog={r['id']:r for r in evidence['complexes']}
            for row in annotated:
                row['reference_complex_address']=catalog.get(row.get('identity_complex_id'),{}).get('address')
                row['reference_house_address']=catalog.get(row.get('identity_house_id'),{}).get('address')
            return {"rows": annotated,
                    "held_property_ids": sorted({r['property_id'] for r in holds}),
                    "evidence_counts": {k:len(v) for k,v in evidence.items()},
                    "membership_rows": len(memberships), "database_writes": 0}
    finally:
        await connection.close()


def _default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value,Decimal) and value.is_finite():
        return int(value) if value==value.to_integral_value() else float(value)
    raise TypeError("Unsupported export value type")


def write_private(path, document):
    path = Path(path)
    if path.is_symlink() or path.exists():
        raise ValueError("Private audit exports are immutable; use a new filename")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False, default=_default).encode("utf-8")
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    return {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-checks", type=Path, help="Optional fresh normal-browser receipts; never inherited from catalog")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    checks = {}
    if args.source_checks:
        document = json.loads(args.source_checks.read_text(encoding="utf-8-sig"))
        if document.get("method") != "normal_browser":
            raise ValueError("Normal-browser evidence required")
        checks = {str(r["id"]):r for r in document["items"]}
        if len(checks) != len(document["items"]):
            raise ValueError("Repeated source receipts")
    from dotenv import dotenv_values
    dsn = os.environ.get("DATABASE_URL") or dotenv_values(args.root/".env", interpolate=False).get("DATABASE_URL")
    if not dsn:
        raise ValueError("Database configuration required; never print connection details")
    snapshot = asyncio.run(read_sale_slice(dsn, now))
    result = build_sale_editorial_slice(snapshot.pop("rows"), now=now, browser_checks=checks,
                                       held_property_ids=snapshot.pop('held_property_ids'))
    result["read_only_audit"] = snapshot
    result['implementation_sha256'] = {
        str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (Path(__file__), ROOT/'bot/core/sale_editorial_slice.py',
                  ROOT/'bot/core/daily_pick_identity.py', ROOT/'bot/core/complex_membership.py',
                  ROOT/'scripts/publish_daily_picks.py')}
    result["exported_at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps({"status":"exported_private", "counts":result["counts"], "database_writes":0,
                      **write_private(args.output, result)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
