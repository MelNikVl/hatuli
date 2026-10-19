"""Private sale identity audit and comparable cohorts; no I/O or writes.

This is a current observation audit, not a transaction-price model. Bootstrap
property IDs are reported separately from confirmed physical identities.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math
import statistics
import hashlib
import json

from bot.core.apartment_selection_reasons import FINISH_LEVELS
from bot.core.daily_pick_identity import normalize_daily_pick_identity, _single_source_house
from bot.core.daily_picks import _valid_source
from bot.core.house_resolution import _address_street_key, _extract_house_number
from scripts.publish_daily_picks import source_identity_rows

MAX_ROWS = 100_000
AREA_ANCHORS = (35, 45, 55, 65, 75, 85, 100, 120)
# A district name embedded in an address or advertisement is not evidence of
# that district. Only complete, explicitly supported field values are allowed.
_DISTRICT_ALIASES = {}
for _slug, _label, _aliases in (
    ("esil", "Есиль", ("Есиль", "Есильский")),
    ("nura", "Нура", ("Нура",)),
    ("almaty", "Алматы", ("Алматы", "Алматинский")),
    ("saraishyk", "Сарайшык", ("Сарайшык", "Сарайшық")),
    ("saryarka", "Сарыарка", ("Сарыарка", "Сарыаркинский")),
    ("baikonur", "Байконур", ("Байконур", "Байқоңыр")),
):
    for _alias in _aliases:
        for _value in (_alias, f"{_alias} р-н", f"{_alias} район",
                       f"р-н {_alias}", f"район {_alias}"):
            _DISTRICT_ALIASES[" ".join(_value.casefold().split())] = (_slug, _label)


def district_key(value):
    """Return an explicit whole-field district alias, never a substring match."""
    if not isinstance(value, str):
        return None
    return _DISTRICT_ALIASES.get(" ".join(value.casefold().split()))


# A negative observation never expires merely because a stored name was later
# overwritten. Removing this hold requires a separately reviewed correction.
KNOWN_SOURCE_CONFLICTS = ({
    "id": "1012143050", "source_url": "https://krisha.kz/a/show/1012143050",
    "source_complex_name": "Акерке 2", "catalog_name": "Возрождение",
    "catalog_id": 3119, "observed_date": "2026-10-19",
    "time_precision": "date_only_for_source_complex_name",
    "listing_checked_at": "2026-10-19T03:18:21.462+00:00",
    "listing_receipt_sha256": "fc643f2f23cf867827bce9331a22abdf71c64c093732e9e835b6295de661e05b",
    "listing_receipt_contains_source_complex_name": False,
    "issue_url": "https://github.com/MelNikVl/hatuli/issues/81",
    "review_document": "docs/source_complex_guard_20261019.md",
    "status": "unresolved_negative_evidence", "method": "normal_browser",
},)


def _time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Timezone-aware observation required")
    return value.astimezone(timezone.utc)


def _number(value):
    if type(value) not in (int, float, Decimal):
        return None
    if isinstance(value,Decimal) and not value.is_finite():
        return None
    try:
        number=float(value)
    except (ValueError,OverflowError):
        return None
    return number if math.isfinite(number) else None


def reference_address_status(row, safe):
    if safe is None or safe['complex_identity']['status']=='source_address_only':
        return 'catalog_reference_unavailable'
    reference = row.get('reference_house_address' if safe['complex_identity']['status']=='house_from_source' else 'reference_complex_address')
    if not _single_source_house(reference) or not _single_source_house(row.get('address')):
        return 'catalog_reference_unavailable'
    source_street, ref_street = _address_street_key(row['address']), _address_street_key(reference)
    source_house, ref_house = _extract_house_number(row['address']), _extract_house_number(reference)
    if source_street != ref_street:
        return 'street_spelling_unresolved'
    return 'exact_database_reference_agreement' if source_house==ref_house else 'house_number_conflict'


def audit_reasons(row, now, *, known_ids):
    """Overlapping diagnostics. A deterministic first reason is also exported."""
    reasons = []
    if str(row.get("id")) in known_ids:
        reasons.append("known_source_conflict")
    if row.get("market_type") != "secondary":
        reasons.append("not_secondary")
    if row.get("is_active") is not True or row.get("archived_at") is not None:
        reasons.append("inactive_or_archived")
    try:
        if not now-timedelta(hours=72) <= _time(row.get("last_seen")) <= now:
            reasons.append("stale_or_future")
    except (ValueError, TypeError):
        reasons.append("stale_or_future")
    if not _valid_source(row):
        reasons.append("invalid_source_url")
    if row.get("is_duplicate") is True or row.get("duplicate_of"):
        reasons.append("known_duplicate")
    if row.get("dup_needs_review") is True:
        reasons.append("duplicate_review_pending")
    for flag, label in (("identity_name_ambiguous", "ambiguous_complex_name"),
                        ("identity_source_name_conflict", "stored_source_name_conflict"),
                        ("identity_complex_review_pending", "complex_review_pending"),
                        ("identity_property_match_pending", "property_review_pending")):
        if row.get(flag) is True:
            reasons.append(label)
    if row.get("property_identity_status") == "review_pending" or row.get("property_link_count", 0) > 1:
        reasons.append("property_review_pending")
    if not _single_source_house(row.get("address")):
        reasons.append("address_missing_or_ambiguous")
    if not district_key(row.get("district")):
        reasons.append("district_unknown")
    price, area = _number(row.get("price")), _number(row.get("area"))
    if price is None or not 5_000_000 <= price <= 100_000_000:
        reasons.append("outside_price_budget")
    if (area is None or not 20 <= area <= 300 or type(row.get("rooms")) is not int
            or not 1 <= row["rooms"] <= 6 or not 14 <= area / row["rooms"] <= 100
            or price is not None and not 150_000 <= price / area <= 3_000_000):
        reasons.append("invalid_area_rooms_or_sqm_price")
    safe = normalize_daily_pick_identity(row)
    if safe is None:
        umbrella = row.get("identity_complex_is_umbrella") is True
        if umbrella and not (row.get("identity_house_valid") is True and row.get("house_attribution") in {"address", "token"}):
            reasons.append("umbrella_house_unresolved")
        reasons.append("identity_gate_failed")
    elif safe.get("complex_id") is not None and row.get("membership_complex_id") != safe.get("complex_id"):
        # A resolved child must be compared with the child, not also umbrella.
        expected = row.get("identity_complex_id") or row.get("identity_house_id")
        if row.get("membership_complex_id") != expected:
            reasons.append("canonical_membership_disagreement")
    address_status = reference_address_status(row, safe)
    if safe is not None:
        safe.update(address_reference_status=address_status,price=price,area=area)
    if address_status=='house_number_conflict':
        reasons.append('catalog_house_number_conflict')
    return list(dict.fromkeys(reasons)), safe


def _stats(rows):
    prices = [r["price"] for r in rows]
    sqm = [r["price"] / r["area"] for r in rows]
    p25, _, p75 = statistics.quantiles(prices, n=4, method="inclusive")
    sq25, _, sq75 = statistics.quantiles(sqm, n=4, method="inclusive")
    return {"median_price": round(statistics.median(prices)), "p25_price": round(p25), "p75_price": round(p75),
            "median_price_per_m2": round(statistics.median(sqm)),
            "p25_price_per_m2": round(sq25), "p75_price_per_m2": round(sq75),
            "n_observations": len(rows), "n_distinct_property_ids": len({r["property_id"] for r in rows if r.get("property_id") is not None}),
            "n_confirmed_physical": len({r["property_id"] for r in rows if r.get("property_identity_status") == "confirmed" and r.get("property_id") is not None}),
            "n_unique_source_after_conservative_dedup": len(rows),
            "source_oldest_at": min(_time(r["last_seen"]) for r in rows).isoformat(),
            "source_newest_at": max(_time(r["last_seen"]) for r in rows).isoformat(),
            "listing_ids": [str(r["id"]) for r in rows]}


def profiles(rows, *, minimum=10, confirmed_only=True):
    """Joint, condition-matched cohorts; fixed area anchors with ±15% bands.

    Overlapping anchors are not additive populations. A row enters once per
    profile. No name-only project assignment or synthetic physical merge.
    """
    groups = defaultdict(list)
    for row in rows:
        if row.get("finish_level") not in FINISH_LEVELS:
            continue
        if confirmed_only and (row.get("property_identity_status") != "confirmed"
                               or type(row.get("property_id")) is not int or row["property_id"] <= 0):
            continue
        if confirmed_only and row.get('address_reference_status')!='exact_database_reference_agreement':
            continue
        district = district_key(row.get("district"))
        if district is None:
            continue
        scopes = [("district", district[0], district[1])]
        identity = row["complex_identity"]
        if (identity["status"] in {"catalog_bound", "house_from_source"}
                and row.get('address_reference_status')=='exact_database_reference_agreement'):
            scopes.append(("house" if identity["status"] == "house_from_source" else "complex",
                           identity["canonical_id"], identity["canonical_name"]))
        for anchor in AREA_ANCHORS:
            if anchor * .85 <= row["area"] <= anchor * 1.15:
                for scope, identifier, label in scopes:
                    groups[(scope, str(identifier), label, row["rooms"], anchor, row["finish_level"])].append(row)
    result = []
    for (scope, identifier, label, rooms, anchor, finish), values in sorted(groups.items()):
        if len(values) < minimum:
            continue
        result.append({"scope": scope, "scope_id": identifier, "label": label, "rooms": rooms,
                       "area_anchor_m2": anchor, "area_min_m2": round(anchor*.85, 2),
                       "area_max_m2": round(anchor*1.15, 2), "finish_level": finish,
                       "ready": confirmed_only, "ranking_ready": confirmed_only,
                       "quality_status": "confirmed_database_physical_keys" if confirmed_only else "physical_uniqueness_not_fully_confirmed",
                       "verification": "database_validated_not_fresh_browser_verified",
                       **_stats(values)})
    return result


def build_sale_editorial_slice(rows, *, now, browser_checks=None, minimum=10, held_property_ids=()):
    now = _time(now)
    if len(rows) > MAX_ROWS:
        raise ValueError("Incomplete bounded sale slice")
    if type(minimum) is not int or minimum < 10:
        raise ValueError("Minimum cohort size must be at least ten")
    if len({str(r.get("id")) for r in rows}) != len(rows):
        raise ValueError("Repeated listing rows; join multiplicity must not inflate cohorts")
    checks = browser_checks or {}
    for record in checks.values():
        if not now-timedelta(hours=2) <= _time(record["checked_at"]) <= now:
            raise ValueError("Browser evidence must be fresh; do not renew old timestamps")
    source_eligible = {str(r["id"]) for r in source_identity_rows(rows, checks)}
    known_ids = {r["id"] for r in KNOWN_SOURCE_CONFLICTS}
    decisions, accepted, exclusions, primary, quality = [], [], Counter(), Counter(), Counter()
    for row in sorted(rows, key=lambda r: str(r["id"])):
        reasons, safe = audit_reasons(row, now, known_ids=known_ids)
        if str(row["id"]) not in source_eligible:
            reasons.append("fresh_browser_source_mismatch")
        reasons = list(dict.fromkeys(reasons))
        if row.get("finish_level") not in FINISH_LEVELS:
            quality["condition_missing"] += 1
        if row.get("property_identity_status") != "confirmed":
            quality["physical_identity_not_confirmed"] += 1
        decision = {"id": str(row["id"]), "accepted": not reasons, "reasons": reasons,
                    "verification": "fresh_browser_verified" if str(row["id"]) in checks and not reasons else "database_validated_only"}
        decisions.append(decision)
        if reasons:
            exclusions.update(reasons)
            primary[reasons[0]] += 1
        else:
            accepted.append(safe)
    # Remove all other publications of a known conflicting physical ID too.
    held_properties = ({r.get("property_id") for r in rows if str(r["id"]) in known_ids}
                       | set(held_property_ids)) - {None}
    dedup, seen = [], set()
    decision_by_id = {r['id']:r for r in decisions}
    for row in sorted(accepted, key=lambda r: (-_time(r["last_seen"]).timestamp(), str(r["id"]))):
        pid = row.get("property_id")
        reason = "known_source_conflict_property" if pid in held_properties else (
            "duplicate_confirmed_physical_property" if row.get("property_identity_status") == "confirmed"
            else "duplicate_property_key_unconfirmed") if pid is not None and pid in seen else None
        if reason:
            exclusions[reason] += 1
            primary[reason] += 1
            decision = decision_by_id[str(row["id"])]
            decision.update(accepted=False, reasons=[reason])
            continue
        if pid is not None:
            seen.add(pid)
        dedup.append(row)
    ready = profiles(dedup, minimum=minimum, confirmed_only=True)
    preview = profiles(dedup, minimum=minimum, confirmed_only=False)
    return {"schema_version": 1, "kind": "private_sale_editorial_slice", "generated_at": now.isoformat(),
            "policy": {"market_type": "secondary", "maximum_price": 100_000_000, "window_hours": 72,
                       "minimum_profile_n": minimum, "area_band_pct": 15, "condition_matched": True,
                       "ready_requires_confirmed_physical_identity": True, "publication_rights": "not_cleared"},
            "known_source_conflicts": list(KNOWN_SOURCE_CONFLICTS),
            "held_property_ids": sorted(held_properties),
            "known_source_conflicts_sha256": hashlib.sha256(json.dumps(KNOWN_SOURCE_CONFLICTS,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest(),
            "counts": {"input_rows": len(rows), "accepted_after_identity_and_dedup": len(dedup),
                       "input_market_types": dict(Counter(str(r.get('market_type')) for r in rows)),
                       "excluded_rows": len(rows)-len(dedup), "exclusion_reasons_overlapping": dict(exclusions),
                       "exclusion_primary_reasons": dict(primary), "quality_diagnostics_overlapping": dict(quality),
                       "accepted_property_identity_statuses": dict(Counter(str(r.get('property_identity_status')) for r in dedup)),
                       "accepted_scope_counts": dict(Counter(r['complex_identity']['status'] for r in dedup)),
                       "accepted_reference_address_statuses": dict(Counter(r['address_reference_status'] for r in dedup)),
                       "ready_profiles": len(ready), "descriptive_cohorts": len(preview)},
            "decisions": decisions, "input_rows": rows, "accepted_rows": dedup,
            "ready_profiles": ready, "descriptive_cohorts": preview,
            "limits": ["No database identities, aliases or source observations were changed.",
                       "Unvisited rows are database validated, not fresh browser verified.",
                       "Provisional property IDs do not prove complete physical deduplication; preview cohorts are not ready.",
                       "Database condition labels are matched, not a visual quality assessment.",
                       "Area-anchor cohorts overlap and must not be summed or ranked as a complete market.",
                       "Primary units require their own evidence/readiness audit and are not combined with secondary prices."]}
