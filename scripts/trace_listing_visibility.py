#!/usr/bin/env python3
"""scripts/trace_listing_visibility.py — read-only diagnostic (задача
"Krisha market coverage audit", Phase 5). Отвечает на один вопрос:
"я вижу это объявление на krisha.kz, но не вижу его на dashboard — почему?"

Строго READ-ONLY: только SELECT (bot.db.pg.fetchrow/fetch), ни одного
execute()/UPDATE/INSERT/DELETE где-либо в этом файле.

Что проверяет:
  1. Есть ли listing_id в apartment_listings (продажа) и/или rental_listings
     (аренда) — listing_id уникален для Крыши в целом, но эти две таблицы
     физически независимы (нет общего UNIQUE constraint между ними), так
     что теоретически один и тот же численный id мог быть присвоен разным
     объявлениям в разное время — проверяем ОБЕ таблицы, не одну.
  2. is_active / last_seen / архивный статус.
  3. Координаты — свои или (для аренды) резолвятся каскадом ЖК -> район.
  4. Проходит ли WHERE-условие реального dashboard-запроса
     (/admin/api/map-points, terminal_extras.py) — условия СКОПИРОВАНЫ
     буквально оттуда (не переизобретены), с датой копирования в
     комментарии у каждого блока, чтобы дрейф было легко заметить при
     следующем ревью.
  5. Если проходит WHERE — попадает ли в первые LIMIT строк выдачи
     (sale: до 15000 по effective_score DESC; rental: до 1000 по
     found_at DESC) — считает точную позицию в ranking, не гадает.

Использование:
    venv/bin/python scripts/trace_listing_visibility.py <listing_id>
    venv/bin/python scripts/trace_listing_visibility.py <listing_id> --json

Работает и с "1234567", и с полным URL "https://krisha.kz/a/show/1234567".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_ID_RE = re.compile(r"/(\d{5,})")


def _extract_id(raw: str) -> str:
    s = raw.strip()
    if s.isdigit():
        return s
    m = _ID_RE.search(s)
    return m.group(1) if m else s


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


def _age_str(dt) -> str | None:
    if dt is None:
        return None
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    days = delta.total_seconds() / 86400
    return f"{days:.1f} days ago"


CAUSE_INTENTIONAL_FILTER = "intentional_filter"
CAUSE_BAD_COORDS = "bad_or_missing_coordinates"
CAUSE_BACKEND_LIMIT = "backend_limit"
CAUSE_STALE = "stale_or_inactive_state"
CAUSE_NOT_FOUND = "not_in_db"
CAUSE_UNKNOWN = "unknown"
CAUSE_VISIBLE = "visible"


async def _trace_sale(listing_id: str) -> dict:
    from bot.db.pg import fetchrow, fetchval

    row = await fetchrow("SELECT * FROM apartment_listings WHERE id = $1", listing_id)
    if row is None:
        return {"in_db": False}
    l = dict(row)

    is_active = l.get("is_active") is not False  # тот же "IS NOT FALSE", что и в dashboard-запросе
    is_dup = bool(l.get("is_duplicate"))
    has_coords = l.get("lat") is not None and l.get("lon") is not None
    last_seen = l.get("last_seen")
    stale_14d = last_seen is None or (
        (datetime.now(timezone.utc) - (last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=timezone.utc)))
        > __import__("datetime").timedelta(days=14)
    )

    findings: list[dict] = []
    if not is_active:
        findings.append({
            "category": CAUSE_STALE,
            "reason": f"is_active = FALSE (archive_reason={l.get('archive_reason')!r}, "
                      f"archived_at={_iso(l.get('archived_at'))})",
        })
    if is_dup:
        findings.append({
            "category": CAUSE_INTENTIONAL_FILTER,
            "reason": f"is_duplicate = TRUE (duplicate_of={l.get('duplicate_of')!r}, "
                      f"dup_match={l.get('dup_match')!r}) — dashboard excludes duplicates by design",
        })
    if not has_coords:
        findings.append({
            "category": CAUSE_BAD_COORDS,
            "reason": "lat/lon IS NULL — no marker can be placed on the map. "
                      "Coordinates only come from the detail-page fetch "
                      "(bot.core.apartment_details), a bounded batch per cycle "
                      "(app_settings.DETAIL_FETCH_BATCH) — a newly discovered "
                      "listing can sit without coordinates for one or more cycles.",
        })
    if is_active and not is_dup and has_coords and stale_14d:
        findings.append({
            "category": CAUSE_STALE,
            "reason": f"last_seen={_iso(last_seen)} ({_age_str(last_seen)}) is older than the "
                      "dashboard's hard cutoff (WHERE a.last_seen > now() - interval '14 days', "
                      "terminal_extras.py::map_points) — marked is_active in DB, but the map "
                      "query itself excludes it regardless.",
        })

    # ── WHERE-условие /admin/api/map-points (type=sale), скопировано из
    # terminal_extras.py::map_points, дата сверки — при аудите Krisha
    # market coverage (см. commit этого файла). Если условие там
    # поменяется, эта копия должна быть обновлена вместе с ним. ─────────
    passes_where = is_active and not is_dup and has_coords and not stale_14d

    rank_info = None
    if passes_where:
        # Порядок по умолчанию — effective_score DESC (cheapest_only=False,
        # самый частый путь открытия дашборда). Считаем ТОЧНУЮ позицию,
        # не гадаем: сколько строк, удовлетворяющих тому же WHERE, стоят
        # выше по рангу.
        higher_ranked = await fetchval(
            """
            SELECT count(*) FROM apartment_listings a
            WHERE a.lat IS NOT NULL AND a.lon IS NOT NULL
              AND a.is_active IS NOT FALSE
              AND COALESCE(a.is_duplicate, FALSE) = FALSE
              AND a.last_seen > now() - interval '14 days'
              AND a.effective_score > (SELECT effective_score FROM apartment_listings WHERE id = $1)
            """,
            listing_id,
        )
        rank_position = int(higher_ranked or 0) + 1
        within_limit = rank_position <= 15000
        rank_info = {"rank_by_effective_score_desc": rank_position, "within_15000_limit": within_limit}
        if not within_limit:
            findings.append({
                "category": CAUSE_BACKEND_LIMIT,
                "reason": f"passes all WHERE filters, but ranks #{rank_position} by effective_score "
                          "DESC — beyond the dashboard's LIMIT 15000 (terminal_extras.py::map_points). "
                          "Frontend batches through offsets but never requests past this cap.",
            })

    if not findings:
        findings.append({"category": CAUSE_VISIBLE, "reason": "passes all backend WHERE filters and "
                          "ranks within the LIMIT — should be visible on the sale map "
                          "(modulo any optional UI filter the user has toggled: rooms/price/"
                          "area/market/seller/finish/floorplan/viewport, and modulo the "
                          "public-tier restriction if not logged in as admin/subscriber)."})

    return {
        "in_db": True,
        "table": "apartment_listings",
        "id": l["id"], "url": l.get("url"), "price": l.get("price"),
        "market_type": l.get("market_type"), "is_active": is_active,
        "is_duplicate": is_dup, "duplicate_of": l.get("duplicate_of"),
        "lat": l.get("lat"), "lon": l.get("lon"),
        "first_seen": _iso(l.get("first_seen")), "last_seen": _iso(l.get("last_seen")),
        "last_seen_age": _age_str(last_seen),
        "archived_at": _iso(l.get("archived_at")), "archive_reason": l.get("archive_reason"),
        "archive_checked_at": _iso(l.get("archive_checked_at")),
        "passes_dashboard_where": passes_where,
        "rank_info": rank_info,
        "findings": findings,
    }


async def _trace_rental(listing_id: str) -> dict:
    from bot.db.pg import fetchrow, fetchval

    row = await fetchrow("SELECT * FROM rental_listings WHERE id = $1", listing_id)
    if row is None:
        return {"in_db": False}
    l = dict(row)

    is_active = l.get("is_active") is not False
    is_dup = bool(l.get("is_duplicate"))
    last_seen = l.get("last_seen")
    archived_at = l.get("archived_at")
    now = datetime.now(timezone.utc)

    def _within(dt, days):
        if dt is None:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds() <= days * 86400

    # ── OR-условие /admin/api/map-points (type=rental), terminal_extras.py
    # ("(is_active AND last_seen>30d) OR (archived AND archived_at>30d)") ──
    time_ok = (is_active and _within(last_seen, 30)) or ((not is_active) and _within(archived_at, 30))

    findings: list[dict] = []
    if is_dup:
        findings.append({
            "category": CAUSE_INTENTIONAL_FILTER,
            "reason": f"is_duplicate = TRUE (duplicate_of={l.get('duplicate_of')!r}) — "
                      "dashboard excludes duplicates by design",
        })
    if not is_active and not archived_at:
        findings.append({
            "category": CAUSE_UNKNOWN,
            "reason": "is_active = FALSE but archived_at is NULL — inconsistent row, "
                      "cannot evaluate the 30-day archived-window condition honestly",
        })
    if not time_ok:
        if is_active:
            findings.append({
                "category": CAUSE_STALE,
                "reason": f"is_active=TRUE but last_seen={_iso(last_seen)} ({_age_str(last_seen)}) "
                          "is older than 30 days — excluded by the map query's time window",
            })
        else:
            findings.append({
                "category": CAUSE_STALE,
                "reason": f"archived (archive_reason={l.get('archive_reason')!r}, "
                          f"archived_at={_iso(archived_at)}, {_age_str(archived_at)}) and older "
                          "than the 30-day archived-window the map still shows",
            })

    # ── Координатный каскад rental (terminal_extras.py::map_points,
    # type=rental): own lat/lon -> complex centroid -> district centroid
    # -> dropped (no_geo). ────────────────────────────────────────────
    coord_source = None
    if l.get("lat") is not None and l.get("lon") is not None:
        coord_source = "own"
    else:
        cx = (l.get("complex_name") or "").strip().lower()
        if cx:
            cx_row = await fetchrow(
                "SELECT AVG(lat) AS lat, AVG(lon) AS lon FROM apartment_listings "
                "WHERE lat IS NOT NULL AND lower(trim(complex_name)) = $1",
                cx,
            )
            if cx_row and cx_row["lat"] is not None:
                coord_source = "complex_centroid"
        if coord_source is None and l.get("district"):
            d_row = await fetchrow(
                "SELECT AVG(lat) AS lat, AVG(lon) AS lon FROM apartment_listings "
                "WHERE lat IS NOT NULL AND district = $1",
                l["district"],
            )
            if d_row and d_row["lat"] is not None:
                coord_source = "district_centroid"

    if coord_source is None:
        findings.append({
            "category": CAUSE_BAD_COORDS,
            "reason": "no own lat/lon, no matching complex_name centroid, no matching "
                      "district centroid — dropped from the rental map entirely (no_geo).",
        })

    passes_where = (not is_dup) and time_ok and coord_source is not None

    rank_info = None
    if passes_where:
        # ORDER BY r.found_at DESC LIMIT 1000 — считаем позицию.
        higher_ranked = await fetchval(
            """
            SELECT count(*) FROM rental_listings r
            WHERE COALESCE(r.is_duplicate, FALSE) = FALSE
              AND (
                (r.is_active IS NOT FALSE AND r.last_seen > now() - interval '30 days')
                OR (r.is_active = FALSE AND r.archived_at > now() - interval '30 days')
              )
              AND r.found_at > (SELECT found_at FROM rental_listings WHERE id = $1)
            """,
            listing_id,
        )
        rank_position = int(higher_ranked or 0) + 1
        within_limit = rank_position <= 1000
        rank_info = {"rank_by_found_at_desc": rank_position, "within_1000_limit": within_limit}
        if not within_limit:
            findings.append({
                "category": CAUSE_BACKEND_LIMIT,
                "reason": f"passes all filters, but ranks #{rank_position} by found_at DESC — "
                          "beyond the rental map's LIMIT 1000 (terminal_extras.py::map_points).",
            })

    if not findings:
        findings.append({"category": CAUSE_VISIBLE, "reason": "passes all backend WHERE filters and "
                          f"ranks within the LIMIT (coord_source={coord_source!r}) — should be "
                          "visible on the map, EXCEPT rental is hidden entirely for public-tier "
                          "(anonymous/unverified) visitors — see bot/core/site_auth.py."})

    return {
        "in_db": True,
        "table": "rental_listings",
        "id": l["id"], "url": l.get("url"), "price": l.get("price"),
        "prop_type": l.get("prop_type"), "is_active": is_active,
        "is_duplicate": is_dup, "duplicate_of": l.get("duplicate_of"),
        "own_lat": l.get("lat"), "own_lon": l.get("lon"), "coord_source": coord_source,
        "found_at": _iso(l.get("found_at")), "last_seen": _iso(l.get("last_seen")),
        "last_seen_age": _age_str(last_seen),
        "archived_at": _iso(archived_at), "archive_reason": l.get("archive_reason"),
        "archive_checked_at": _iso(l.get("archive_checked_at")),
        "passes_dashboard_where": passes_where,
        "rank_info": rank_info,
        "findings": findings,
    }


async def trace(listing_id_or_url: str) -> dict:
    listing_id = _extract_id(listing_id_or_url)
    sale = await _trace_sale(listing_id)
    rental = await _trace_rental(listing_id)

    result = {"input": listing_id_or_url, "resolved_listing_id": listing_id,
              "sale": sale, "rental": rental}

    if not sale.get("in_db") and not rental.get("in_db"):
        result["summary"] = {
            "category": CAUSE_NOT_FOUND,
            "reason": "id not found in apartment_listings or rental_listings — either never "
                      "discovered by our collector, or the id itself is wrong/mistyped. This "
                      "script cannot distinguish those two without a live (read-only) fetch of "
                      "the Krisha page itself, which it deliberately does not do.",
        }
    return result


def _print_human(result: dict) -> None:
    print(f"listing_id: {result['resolved_listing_id']} (input: {result['input']})")
    print()
    for kind in ("sale", "rental"):
        section = result[kind]
        print(f"── {kind} ({'apartment_listings' if kind == 'sale' else 'rental_listings'}) " + "─" * 20)
        if not section.get("in_db"):
            print("  not in DB")
            print()
            continue
        for key in ("is_active", "is_duplicate", "last_seen", "last_seen_age",
                    "archived_at", "archive_reason", "passes_dashboard_where", "rank_info"):
            if key in section:
                print(f"  {key}: {section[key]}")
        if kind == "sale":
            print(f"  lat/lon: {section.get('lat')}, {section.get('lon')}")
        else:
            print(f"  own lat/lon: {section.get('own_lat')}, {section.get('own_lon')}  "
                  f"coord_source: {section.get('coord_source')}")
        print("  findings:")
        for f in section["findings"]:
            print(f"    [{f['category']}] {f['reason']}")
        print()
    if "summary" in result:
        print(f"SUMMARY: [{result['summary']['category']}] {result['summary']['reason']}")


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("listing_id", help="Krisha listing id, or full URL")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv()
    database_url = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

    from bot.db.pg import init_pool, close_pool
    await init_pool(database_url)
    try:
        result = await trace(args.listing_id)
    finally:
        await close_pool()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(result)


if __name__ == "__main__":
    asyncio.run(_main())
