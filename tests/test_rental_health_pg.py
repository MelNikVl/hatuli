"""Aggregate health SQL uses an explicit disposable test DB and temporary rows.

The shared opt-in fixture rejects remote/production destinations and never
initializes the application pool or migrations. No listing/contact projection.
"""
from datetime import timedelta

import pytest

from bot import rental_health_web as health
from tests.test_rental_stage1_pg import database  # noqa: F401; guarded opt-in fixture


@pytest.mark.asyncio
async def test_real_health_separates_periods_conflicts_and_catalog_proof(database):
    await database.execute("""CREATE TEMP TABLE rental_listings (
        prop_type text, city text, rent_period text, rent_period_conflict bool DEFAULT FALSE,
        is_active bool DEFAULT TRUE, archived_at timestamptz, is_duplicate bool DEFAULT FALSE,
        dup_needs_review bool DEFAULT FALSE, duplicate_of text, last_seen timestamptz,
        found_at timestamptz, archive_reason text, complex_name text, complex_url text,
        address text, area numeric, rooms int, floor int, lat double precision, lon double precision,
        photo_url text, photos jsonb, rental_episode_last_seen_at timestamptz,
        description_checked_at timestamptz, coord_fetch_attempted_at timestamptz
    ) ON COMMIT DROP""")
    now = await database.fetchval("SELECT now()")
    recent, old, future = now-timedelta(minutes=5), now-timedelta(days=4), now+timedelta(seconds=1)
    values = [
        ("apartment", "astana", "monthly", False, False, recent),
        ("apartment", "astana", "monthly", False, False, old),  # fresh detail touch is not catalog proof
        ("apartment", "astana", "monthly", False, True, recent),  # duplicate excluded from clean pool
        ("apartment", "astana", "daily", False, False, recent),
        ("apartment", "astana", "daily", False, False, future),  # future proof rejected
        ("apartment", "astana", "unknown", False, False, recent),
        ("apartment", "astana", "monthly", True, False, recent),  # conflict wins over claimed period
        ("apartment", "almaty", "daily", False, False, recent),
        ("parking", "astana", "monthly", False, False, recent),
    ]
    await database.executemany("""INSERT INTO rental_listings
        (prop_type,city,rent_period,rent_period_conflict,is_duplicate,rental_episode_last_seen_at,last_seen)
        VALUES ($1,$2,$3,$4,$5,$6,now())""", values)
    rows = [dict(row) for row in await database.fetch(health.AGGREGATE_SQL, list(health.TYPES))]
    result = health.project_rental_health(rows, {}, now=now)
    monthly, daily, parking, commercial = result["types"]
    assert monthly["total_rows"] == 3 and monthly["clean_active_rows"] == 2
    assert monthly["fresh_72h"] == 2 and monthly["catalog_fresh_72h"] == 1
    assert daily["clean_active_rows"] == 2 and daily["catalog_fresh_72h"] == 1
    assert parking["clean_active_rows"] == 1 and commercial["clean_active_rows"] == 0
    periods = {item["rent_period"]: item for item in result["apartment_periods"]}
    assert periods["unknown"]["clean_active_rows"] == 1 and periods["unknown"]["catalog_fresh_72h"] == 0
    assert periods["conflict"]["clean_active_rows"] == 1 and periods["conflict"]["catalog_fresh_72h"] == 0
    assert sum(item["clean_active_rows"] for item in periods.values()) == 6
    assert result["market_coverage"] == "unknown"


@pytest.mark.asyncio
async def test_full_detail_refresh_rejects_attempts_future_dates_stale_and_duplicates(database):
    await database.execute("""CREATE TEMP TABLE rental_listings (
        prop_type text DEFAULT 'apartment', city text DEFAULT 'astana', rent_period text DEFAULT 'monthly',
        rent_period_conflict bool DEFAULT FALSE, is_active bool DEFAULT TRUE, archived_at timestamptz,
        is_duplicate bool DEFAULT FALSE, dup_needs_review bool DEFAULT FALSE, duplicate_of text,
        last_seen timestamptz DEFAULT now(), found_at timestamptz, archive_reason text,
        complex_name text, complex_url text, address text, area numeric, rooms int, floor int,
        lat double precision, lon double precision, photo_url text, photos jsonb,
        rental_episode_last_seen_at timestamptz DEFAULT now(), description_checked_at timestamptz,
        coord_fetch_attempted_at timestamptz DEFAULT now()
    ) ON COMMIT DROP""")
    now = await database.fetchval('SELECT now()')
    await database.executemany('INSERT INTO rental_listings (description_checked_at,is_duplicate) VALUES ($1,$2)',[
        (now-timedelta(hours=1),False), (None,False),
        (now-timedelta(hours=73),False), (now+timedelta(hours=1),False),
        (now-timedelta(hours=1),True)])
    rows = [dict(row) for row in await database.fetch(health.AGGREGATE_SQL,list(health.TYPES))]
    monthly = health.project_rental_health(rows,{},now=now)['types'][0]
    assert monthly['clean_active_rows'] == monthly['fresh_72h'] == 4
    assert monthly['detail_fresh_72h'] == 1
    assert monthly['detail_stale_72h'] == 3 and monthly['detail_missing'] == 1
    assert monthly['detail_attempted_24h'] == 4
