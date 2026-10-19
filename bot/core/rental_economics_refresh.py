"""Bounded derived-data refresh; callers own scheduling and database startup."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json

from bot.core.rental_economics import build_rental_economics, build_benchmarks, CITY_SCOPE
from bot.core.rental_yield import NetYieldParams


def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      default=lambda item: item.isoformat() if isinstance(item, datetime) else float(item) if isinstance(item, Decimal) else str(item))


async def refresh_batch(*, ids=None, after_id="", limit=500, _lookup_cache=None):
    """Recompute at most 1000 clean sale snapshots, with an optimistic price/identity check.

    Does not initialize a pool, migrate, scrape, send messages or alter Deal
    Score. A missing qualified index clears the scenario instead of keeping a
    legacy yield. The existing parser can refresh its bounded changed-ID batch.
    """
    from bot.db import pg, settings
    from bot.core.rental_parser import lookup_rental_estimate
    if type(limit) is not int or not 1 <= limit <= 1000 or ids is not None and len(ids) > 1000:
        raise ValueError("A refresh batch must contain at most 1000 listings")
    params = NetYieldParams.from_settings(settings.get)
    now = datetime.now(timezone.utc)
    rows = await pg.fetch("""SELECT a.* FROM apartment_listings a
        WHERE a.is_active IS TRUE AND NOT coalesce(a.is_duplicate,false)
          AND NOT coalesce(a.dup_needs_review,false) AND a.archived_at IS NULL
          AND nullif(btrim(a.duplicate_of),'') IS NULL
          AND ($1::text[] IS NULL OR a.id=ANY($1)) AND a.id>$2
        ORDER BY a.id LIMIT $3""", ids, after_id, limit+1)
    cache, changed, skipped = _lookup_cache if _lookup_cache is not None else {}, 0, 0
    for raw in rows[:limit]:
        row = dict(raw)
        cid, name = row.get("complex_id"), row.get("complex_name")
        if row.get("complex_resolution") in ("geo", "unbound") or row.get("house_attribution") in ("geo", "address_geo"):
            cid, name = None, None
        key = (cid, None if cid is not None else name, row.get("district"), row.get("rooms"))
        if key not in cache:
            cache[key] = await lookup_rental_estimate(CITY_SCOPE, row.get("district"), name, row.get("rooms"), complex_id=cid)
        economics = build_rental_economics(row, cache[key], params, now=now)
        derived = (economics["est_rent"], Decimal(str(economics["gross_yield_pct"])), economics["net_yield_pct"],
                   Decimal(str(economics["gross_payback_years"])), f"{economics['rent_source']['level']}, n={economics['rent_source']['n']}") if economics else (None,)*5
        result = await pg.execute("""UPDATE apartment_listings SET rental_economics=$2::jsonb,
            est_rent=$3,yield_pct=$4,net_yield_pct=$5,payback_years=$6,rent_source=$7
            WHERE id=$1 AND price IS NOT DISTINCT FROM $8::bigint
              AND area IS NOT DISTINCT FROM $9::real AND rooms IS NOT DISTINCT FROM $10::integer
              AND complex_id IS NOT DISTINCT FROM $11::integer
              AND complex_name IS NOT DISTINCT FROM $12::text
              AND complex_resolution IS NOT DISTINCT FROM $13::text
              AND resolved_house_id IS NOT DISTINCT FROM $14::integer
              AND house_attribution IS NOT DISTINCT FROM $15::text
              AND district IS NOT DISTINCT FROM $16::text
              AND market_type IS NOT DISTINCT FROM $17::text
              AND is_active IS TRUE AND archived_at IS NULL
              AND NOT coalesce(is_duplicate,false) AND NOT coalesce(dup_needs_review,false)
              AND nullif(btrim(duplicate_of),'') IS NULL""",
            row["id"], json_text(economics) if economics else None, *derived,
            row.get("price"), row.get("area"), row.get("rooms"), row.get("complex_id"), row.get("complex_name"),
            row.get("complex_resolution"), row.get("resolved_house_id"), row.get("house_attribution"), row.get("district"), row.get("market_type"))
        changed += result == "UPDATE 1"
        skipped += result != "UPDATE 1"
    return {"scanned": min(limit, len(rows)), "changed": changed, "optimistic_skipped": skipped,
            "lookup_segments": len(cache), "has_more": len(rows)>limit,
            "next_after_id": rows[min(limit, len(rows))-1]["id"] if rows else after_id}


async def refresh_all(*, batch_size=500, max_batches=100):
    """Bounded full warmup using the caller's existing initialized pool.

    A cycle refreshes old inventory as well as just-parsed IDs. Index lookups
    are memoized by segment across batches; 25k listings do not mean 25k
    index lookups. The cursor and count expose partial work without claiming
    that the entire inventory is ready.
    """
    if type(max_batches) is not int or not 1 <= max_batches <= 1000:
        raise ValueError("Bounded refresh required")
    cursor, cache, total = "", {}, {"scanned": 0, "changed": 0, "optimistic_skipped": 0}
    for _ in range(max_batches):
        result = await refresh_batch(after_id=cursor, limit=batch_size, _lookup_cache=cache)
        for key in total:
            total[key] += result[key]
        if not result["has_more"]:
            return {**total, "complete": True, "lookup_segments": len(cache)}
        if result["next_after_id"] <= cursor:
            raise RuntimeError("Rental snapshot refresh cursor did not advance")
        cursor = result["next_after_id"]
    return {**total, "complete": False, "next_after_id": cursor, "lookup_segments": len(cache)}


async def rebuild_benchmarks():
    """Publish only qualified scenario cohorts; never use stored legacy yield alone."""
    from bot.db import pg, settings
    params = NetYieldParams.from_settings(settings.get)
    now = datetime.now(timezone.utc)
    rows = await pg.fetch("""SELECT a.*,pl.property_id,c.housing_class AS rental_housing_class
        FROM apartment_listings a LEFT JOIN property_listings pl ON pl.listing_id=a.id
        LEFT JOIN complexes c ON c.id=coalesce(a.resolved_house_id,a.complex_id)
          AND a.complex_resolution IS DISTINCT FROM 'geo' AND a.complex_resolution IS DISTINCT FROM 'unbound'
          AND a.house_attribution IS DISTINCT FROM 'geo' AND a.house_attribution IS DISTINCT FROM 'address_geo'
          AND NOT coalesce(c.is_garbage,false) AND NOT coalesce(c.is_street,false)
          AND NOT coalesce(c.is_umbrella,false) AND c.canonical_reason IS DISTINCT FROM 'junk_unmatched'
        WHERE a.is_active IS TRUE AND a.archived_at IS NULL AND NOT coalesce(a.is_duplicate,false)
          AND NOT coalesce(a.dup_needs_review,false) AND nullif(btrim(a.duplicate_of),'') IS NULL
          AND a.rental_economics IS NOT NULL AND a.last_seen BETWEEN $1::timestamptz-interval '72 hours' AND $1
        ORDER BY a.last_seen DESC,a.id""", now)
    benchmarks = build_benchmarks([dict(row) for row in rows], params=params, now=now)
    async with pg.get_pool().acquire() as connection:
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "rental_yield_benchmarks:astana:v1")
            for item in benchmarks:
                await connection.execute("""INSERT INTO rental_yield_benchmarks
                    (scope,scope_value,rooms,median_net,p90_net,n,net_values,computed_at,source_oldest_at,source_newest_at,params_used,policy_version)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12)
                    ON CONFLICT ON CONSTRAINT rental_yield_benchmarks_key DO UPDATE SET
                      median_net=EXCLUDED.median_net,p90_net=EXCLUDED.p90_net,n=EXCLUDED.n,net_values=EXCLUDED.net_values,
                      computed_at=EXCLUDED.computed_at,source_oldest_at=EXCLUDED.source_oldest_at,
                      source_newest_at=EXCLUDED.source_newest_at,params_used=EXCLUDED.params_used,policy_version=EXCLUDED.policy_version""",
                    *(item[field] for field in ("scope", "scope_value", "rooms", "median_net", "p90_net", "n", "net_values", "computed_at", "source_oldest_at", "source_newest_at")),
                    json_text(asdict(params)), "3")
            await connection.execute("DELETE FROM rental_yield_benchmarks WHERE computed_at<$1 OR policy_version IS DISTINCT FROM '3'", now)
    return {"groups": len(benchmarks), "qualified_listings": sum(item["n"] for item in benchmarks if item["scope"] == "city")}
