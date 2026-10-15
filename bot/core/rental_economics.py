"""Qualified monthly asking-rent scenarios shared by cards, maps and buyer bot.

The application database contains Astana sales; apartment_listings has no
city column. The rental index is explicitly scoped to Astana. Nothing here
establishes an achieved contract rent, occupancy, tax regime or future return.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import math
import statistics

from bot.core.rental_parser import _rental_index_result_ready, _rental_index_thresholds, _extract_district
from bot.core.rental_yield import NetYieldParams, monthly_yield_scenario

SCHEMA_VERSION = 1
CITY_SCOPE = "astana"
SOURCE_TTL = timedelta(hours=72)
BENCHMARK_TTL = timedelta(hours=26)
CONFIDENCE = {"low": 0, "medium": 1, "high": 2}


def utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, OverflowError):
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def number(value, low, high):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return value if math.isfinite(value) and low <= value <= high else None


def facts(listing):
    return {key: listing.get(key) for key in ("id", "price", "area", "rooms", "district", "market_type", "complex_id", "complex_name",
                                             "complex_resolution", "resolved_house_id", "house_attribution")}


def clean_sale(listing, now):
    last_seen = utc(listing.get("last_seen"))
    return (listing.get("is_active") is True and listing.get("archived_at") is None and listing.get("market_type") != "primary"
            and listing.get("is_duplicate") in (False, None) and listing.get("dup_needs_review") in (False, None)
            and not listing.get("duplicate_of") and last_seen is not None and now-SOURCE_TTL <= last_seen <= now
            and number(listing.get("price"), 1_000_000, 2_000_000_000) is not None
            and number(listing.get("area"), 20, 300) is not None and type(listing.get("rooms")) is int
            and 1 <= listing["rooms"] <= 6)


def confidence(level):
    return "high" if level == "ЖК" else "medium" if level == "район" else "low"


def build_rental_economics(listing, index, params, *, now=None, benchmark=None, rental_comps=None):
    now = utc(now or datetime.now(timezone.utc))
    if now is None or not isinstance(listing, dict) or not clean_sale(listing, now) or not isinstance(index, dict):
        return None
    level = index.get("level")
    if level not in ("ЖК", "район", "город", "район (все комн.)", "город (все комн.)"):
        return None
    min_complex, min_area = _rental_index_thresholds()
    if not _rental_index_result_ready(index, now, min_complex if level == "ЖК" else min_area):
        return None
    if not isinstance(params, NetYieldParams):
        return None
    per_m2 = number(index.get("price_per_sqm"), 100, 100000)
    rent = round(per_m2*float(listing["area"])) if per_m2 is not None else index["median_price"]
    if not 50_000 <= rent <= 2_000_000:
        return None
    scenario = monthly_yield_scenario(float(listing["price"]), rent, params)
    if scenario is None:
        return None
    source = {"level": level, "n": index["sample_count"], "updated_at": utc(index["updated_at"]).isoformat(),
              "source_oldest_at": utc(index["source_oldest_at"]).isoformat(),
              "source_newest_at": utc(index["source_newest_at"]).isoformat(), "window_days": index["window_days"], "policy_version": "3",
              "median_price": index["median_price"], "p25_price": index["p25_price"], "p75_price": index["p75_price"],
              "updated_epoch": utc(index["updated_at"]).timestamp(), "oldest_epoch": utc(index["source_oldest_at"]).timestamp(),
              "newest_epoch": utc(index["source_newest_at"]).timestamp()}
    result = {"schema_version": SCHEMA_VERSION, "city_scope": CITY_SCOPE, "computed_at": now.isoformat(), "computed_epoch": now.timestamp(),
              "listing": facts(listing), "est_rent": rent,
              "rent_range": [round(rent*index["p25_price"]/index["median_price"]), round(rent*index["p75_price"]/index["median_price"])],
              "rent_source": source, "confidence": confidence(level), "gross_yield_pct": scenario["gross_yield_pct"],
              "net_yield_pct": scenario["net_yield_pct"], "payback_years": scenario["net_payback_years"],
              "gross_payback_years": scenario["gross_payback_years"], "params_used": asdict(params),
              "asking_prices": True, "scenario": True, "rental_comps": list(rental_comps or [])[:5]}
    qualified = qualify_benchmark(benchmark, params, now) if benchmark else None
    if qualified:
        values = qualified.pop("net_values")
        qualified["percentile"] = round(100*sum(value <= scenario["net_yield_pct"] for value in values)/len(values))
        result["benchmark"] = qualified
    result["high_yield"] = bool(result["confidence"] in ("medium", "high") and qualified
                                and result["net_yield_pct"] >= qualified["p90_net"])
    return result


def qualify_benchmark(value, params, now):
    if not isinstance(value, dict) or value.get("policy_version") != "3":
        return None
    computed, oldest, newest = (utc(value.get(field)) for field in ("computed_at", "source_oldest_at", "source_newest_at"))
    raw_params = value.get("params_used")
    if isinstance(raw_params, str):
        try:
            raw_params = json.loads(raw_params)
        except ValueError:
            return None
    values = value.get("net_values")
    if (None in (computed, oldest, newest) or not now-BENCHMARK_TTL <= computed <= now
            or not now-SOURCE_TTL <= oldest <= newest <= computed or raw_params != asdict(params)
            or type(value.get("n")) is not int or value["n"] < 10 or not isinstance(values, (list, tuple))
            or len(values) != value["n"] or any(number(item, -100, 100) is None for item in values)
            or number(value.get("median_net"), -100, 100) is None or number(value.get("p90_net"), -100, 100) is None):
        return None
    ordered = sorted(values)
    position = (len(ordered)-1)*.9
    lower, upper = math.floor(position), math.ceil(position)
    p90 = ordered[lower]+(ordered[upper]-ordered[lower])*(position-lower)
    if abs(value["median_net"]-statistics.median(ordered)) > 1e-8 or abs(value["p90_net"]-p90) > 1e-8:
        return None
    return {key: value.get(key) for key in ("scope", "scope_value", "rooms", "median_net", "p90_net", "n")} | {
        "computed_at": computed.isoformat(), "source_oldest_at": oldest.isoformat(), "source_newest_at": newest.isoformat(),
        "params_used": raw_params, "policy_version": "3", "net_values": list(values)}


def cached_rental_economics(listing, *, now=None, params=None):
    now = utc(now or datetime.now(timezone.utc))
    value = listing.get("rental_economics")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION or value.get("city_scope") != CITY_SCOPE:
        return None
    if value.get("listing") != facts(listing) or value.get("asking_prices") is not True or value.get("scenario") is not True:
        return None
    try:
        used = NetYieldParams(**value["params_used"])
    except (KeyError, TypeError, ValueError):
        return None
    if params is not None and asdict(params) != asdict(used):
        return None
    source = value.get("rent_source") or {}
    if source.get("level") not in ("ЖК", "район", "город", "район (все комн.)", "город (все комн.)"):
        return None
    rent, rent_range = value.get("est_rent"), value.get("rent_range")
    if (not isinstance(rent_range, list) or len(rent_range) != 2 or number(rent, 50000, 2000000) is None
            or any(number(item, 1, 5000000) is None for item in rent_range) or not rent_range[0] <= rent <= rent_range[1]):
        return None
    # Rebuild the source gate without inferring source facts from the scenario's scaled range.
    probe = {"rent_period": "monthly", "policy_version": source.get("policy_version"), "window_days": source.get("window_days"),
             "updated_at": source.get("updated_at"), "source_oldest_at": source.get("source_oldest_at"),
             "source_newest_at": source.get("source_newest_at"), "sample_count": source.get("n"),
             "median_price": source.get("median_price"), "p25_price": source.get("p25_price"), "p75_price": source.get("p75_price")}
    minimum = _rental_index_thresholds()[0 if source.get("level") == "ЖК" else 1]
    computed = utc(value.get("computed_at"))
    if (now is None or computed is None or not now-BENCHMARK_TTL <= computed <= now or not clean_sale(listing, now)
            or not _rental_index_result_ready(probe, now, minimum)
            or utc(source.get("updated_at")) > computed
            or value.get("confidence") != confidence(source.get("level"))):
        return None
    scenario = monthly_yield_scenario(float(listing["price"]), rent, used)
    if (not scenario or value.get("net_yield_pct") != scenario["net_yield_pct"]
            or value.get("gross_yield_pct") != scenario["gross_yield_pct"]
            or value.get("payback_years") != scenario["net_payback_years"]
            or value.get("gross_payback_years") != scenario["gross_payback_years"]
            or rent_range != [round(rent*source["p25_price"]/source["median_price"]), round(rent*source["p75_price"]/source["median_price"])]):
        return None
    # Benchmarks need their own fresh source gate; no stale p90 badge is kept.
    safe = {key: value[key] for key in ("schema_version", "city_scope", "computed_at", "est_rent", "rent_range", "rent_source",
                                       "confidence", "gross_yield_pct", "net_yield_pct", "payback_years", "gross_payback_years",
                                       "params_used", "asking_prices", "scenario") if key in value}
    safe["rental_comps"] = []
    safe["high_yield"] = False
    benchmark = value.get("benchmark")
    if isinstance(benchmark, dict) and (stamp := utc(benchmark.get("computed_at"))) and now-BENCHMARK_TTL <= stamp <= now:
        oldest, newest = utc(benchmark.get("source_oldest_at")), utc(benchmark.get("source_newest_at"))
        if (number(benchmark.get("p90_net"), -100, 100) is not None and type(benchmark.get("n")) is int and benchmark["n"] >= 10
                and oldest is not None and newest is not None and now-SOURCE_TTL <= oldest <= newest <= stamp
                and benchmark.get("params_used") == asdict(used) and benchmark.get("policy_version") == "3"):
            safe["benchmark"] = {key: benchmark.get(key) for key in ("scope", "scope_value", "rooms", "median_net", "p90_net", "n", "percentile", "computed_at")}
            safe["high_yield"] = bool(safe["confidence"] in ("medium", "high") and safe["net_yield_pct"] >= benchmark["p90_net"])
    return safe


async def rental_economics_for_listing(listing, *, now=None):
    from bot.db import settings
    from bot.core.rental_parser import lookup_rental_estimate
    now = utc(now or datetime.now(timezone.utc))
    if now is None or not clean_sale(listing, now):
        return None
    try:
        params = NetYieldParams.from_settings(settings.get)
        cid, name = listing.get("complex_id"), listing.get("complex_name")
        if listing.get("complex_resolution") in ("geo", "unbound") or listing.get("house_attribution") in ("geo", "address_geo"):
            cid, name = None, None
        index = await lookup_rental_estimate(CITY_SCOPE, listing.get("district"), name, listing.get("rooms"), complex_id=cid)
        if not index:
            return None
        benchmark = await benchmark_for_listing(listing, params, now)
        result = build_rental_economics(listing, index, params, now=now, benchmark=benchmark)
        if result:
            result["rental_comps"] = await rental_comps_for_listing(listing)
        return result
    except Exception:
        # Missing migrations, absent data or a failed provider hide the block.
        return None


async def benchmark_for_listing(listing, params, now):
    from bot.db.pg import fetch
    from bot.db.pg import fetchval
    context = dict(listing)
    context["rental_housing_class"] = await fetchval("""SELECT housing_class FROM complexes
        WHERE id=$1 AND NOT coalesce(is_garbage,false) AND NOT coalesce(is_street,false)
          AND NOT coalesce(is_umbrella,false) AND canonical_reason IS DISTINCT FROM 'junk_unmatched'""",
        listing.get("resolved_house_id") or listing.get("complex_id")) if listing.get("complex_resolution") not in ("geo", "unbound") and listing.get("house_attribution") not in ("geo", "address_geo") else None
    rows = await fetch("""SELECT * FROM rental_yield_benchmarks WHERE rooms=$1
        AND computed_at BETWEEN $2::timestamptz-interval '26 hours' AND $2""", listing["rooms"], now)
    for scope, value in benchmark_scopes(context):
        for row in rows:
            row = dict(row)
            if row.get("scope") == scope and row.get("scope_value") == value and qualify_benchmark(row, params, now):
                return row
    return None


def benchmark_scopes(listing):
    district, housing_class = listing.get("district"), listing.get("rental_housing_class")
    return ([("district_class", f"{district} · {housing_class}")] if district and housing_class else []) + (
        [("district", district)] if district else []) + ([("class", housing_class)] if housing_class else []) + [("city", CITY_SCOPE)]


def attach_benchmark(economics, listing, rows, params, now):
    if not economics:
        return economics
    for scope, value in benchmark_scopes(listing):
        for row in rows:
            if row.get("scope") != scope or row.get("scope_value") != value or row.get("rooms") != listing.get("rooms"):
                continue
            qualified = qualify_benchmark(dict(row), params, now)
            if not qualified:
                continue
            values = qualified.pop("net_values")
            qualified["percentile"] = round(100*sum(value <= economics["net_yield_pct"] for value in values)/len(values))
            return {**economics, "benchmark": qualified,
                    "high_yield": economics["confidence"] in ("medium", "high") and economics["net_yield_pct"] >= qualified["p90_net"]}
    return {**economics, "high_yield": False}


async def rental_comps_for_listing(listing):
    from bot.db.pg import fetch
    from bot.core.comparison_rental_estimate import SINGLE_OFFER_RENTAL_SQL
    from bot.core.daily_picks import sanitized_listing_address
    if not listing.get("complex_id") or listing.get("complex_resolution") in ("geo", "unbound"):
        return []
    now = datetime.now(timezone.utc)
    sql=SINGLE_OFFER_RENTAL_SQL.replace("SELECT r.id,r.price,r.area", "SELECT r.id,r.address,r.price,r.area",1)
    rows = await fetch(sql, now, listing["complex_id"], listing["rooms"], float(listing["area"]))
    result, seen = [], set()
    for row in rows:
        identifier = str(row.get("id") or "")
        observed = utc(row.get("last_seen"))
        if (not identifier.isdigit() or not 5 <= len(identifier) <= 20 or identifier in seen
                or observed is None or not now-SOURCE_TTL <= observed <= now
                or row.get("is_active") is not True or row.get("rent_period") != "monthly"
                or row.get("complex_binding_verified") is not True
                or row.get("is_duplicate") is True or row.get("dup_needs_review") is True
                or row.get("archived_at") is not None or row.get("duplicate_of")
                or row.get("rooms") != listing.get("rooms") or number(row.get("price"), 50000, 2000000) is None
                or number(row.get("area"), float(listing["area"])*.85, float(listing["area"])*1.15) is None):
            continue
        seen.add(identifier)
        result.append({"id": identifier, "url": f"https://krisha.kz/a/show/{identifier}", "price": row["price"],
                       "area": row["area"], "rooms": row["rooms"], "observed_at": observed.isoformat()})
        address,_=sanitized_listing_address(row.get("address"),int(row["price"]),now)
        if address:
            result[-1]["address"]=address
        if len(result) == 5:
            break
    return result


def build_benchmarks(rows, *, params, now):
    """Only qualified fresh scenarios and >=2-day independent listing lives."""
    groups, seen = defaultdict(list), set()
    for listing in rows:
        first, last = utc(listing.get("first_seen")), utc(listing.get("last_seen"))
        economics = cached_rental_economics(listing, now=now, params=params)
        key = ("property", listing["property_id"]) if listing.get("property_id") is not None else ("listing", listing["id"])
        if (not economics or economics["confidence"] not in ("medium", "high") or key in seen
                or first is None or last is None or last-first < timedelta(days=2)):
            continue
        seen.add(key)
        rooms = listing["rooms"]
        keys = {("city", CITY_SCOPE, rooms)}
        district, housing_class = listing.get("district"), listing.get("rental_housing_class")
        if district:
            keys.add(("district", district, rooms))
        if housing_class:
            keys.add(("class", housing_class, rooms))
            if district:
                keys.add(("district_class", f"{district} · {housing_class}", rooms))
        for scope in keys:
            groups[scope].append(economics)
    result = []
    for (scope, value, rooms), values in groups.items():
        if len(values) < 10:
            continue
        yields = sorted(item["net_yield_pct"] for item in values)
        position = (len(yields)-1)*.9
        low, high = math.floor(position), math.ceil(position)
        p90 = yields[low]+(yields[high]-yields[low])*(position-low)
        result.append({"scope": scope, "scope_value": value, "rooms": rooms, "median_net": statistics.median(yields),
                       "p90_net": p90, "n": len(values), "net_values": yields, "computed_at": now,
                       "source_oldest_at": min(utc(item["rent_source"]["source_oldest_at"]) for item in values),
                       "source_newest_at": max(utc(item["rent_source"]["source_newest_at"]) for item in values),
                       "params_used": asdict(params), "policy_version": "3"})
    return result
