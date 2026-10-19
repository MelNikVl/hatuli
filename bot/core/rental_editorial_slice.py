"""Private, complete observed monthly-rental cross-section; never a ranking.

Source IDs and precautionary lookalike groups are not physical apartments.
The pure builder makes no database calls, rewrites no identities, and exports
no descriptions, contacts, photographs, or street/flat addresses.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import statistics
from urllib.parse import urlsplit

from bot.core.complex_membership import rental_identity_ctes_sql
from bot.core.daily_pick_identity import annotate_daily_pick_identity_rows, _single_source_house
from bot.core.house_resolution import _address_street_key, _extract_house_number, _same_house_address
from bot.core.rental_data_quality import collection_health
from bot.core.rental_furnishing import classify_furnishing, furnishing_validation_ready, version_tag
from bot.core.rental_period import classify_rental_period

VERSION = "rental_editorial_slice_v1"
MAX_SCAN = 50_000
TTL_HOURS = 72
# Disjoint, exhaustive bins. Every bin lies within +/-15% of its midpoint.
AREA_EDGES = (20, 26, 34, 44, 57, 74, 96, 124, 160, 206, 266, 300)
DISTRICTS = {
    "есиль": ("esil", "Есиль"), "есильский": ("esil", "Есиль"),
    "нура": ("nura", "Нура"), "алматы": ("almaty", "Алматы"),
    "алматинский": ("almaty", "Алматы"), "сарыарка": ("saryarka", "Сарыарка"),
    "сарыаркинский": ("saryarka", "Сарыарка"), "сарайшык": ("saraishyk", "Сарайшык"),
    "сарайшық": ("saraishyk", "Сарайшык"), "байконур": ("baikonur", "Байконур"),
    "байқоңыр": ("baikonur", "Байконур"),
}
_NONMONTHLY = re.compile(r"посуточ|почас|сутк|тәулік|сағат|per\s+(day|night|hour)", re.I)
CONDITIONS = {
    "designer_renovation_claim": r"дизайнерск\w*\s+ремонт\w*",
    "euro_renovation_claim": r"евро\s*ремонт\w*",
    "cosmetic_renovation_claim": r"косметическ\w*\s+ремонт\w*",
    "unrenovated_claim": r"без\s+ремонта|чернов\w*\s+отделк\w*",
}


def utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, OverflowError):
            return None
    return value.astimezone(timezone.utc) if isinstance(value, datetime) and value.tzinfo else None


def district(value):
    """Explicit stored labels only; never assign a district from a centroid."""
    if not isinstance(value, str):
        return None
    value = value.casefold().replace("ё", "е").strip()
    value = re.sub(r"^(?:район\s+|р-н\s+)", "", value)
    value = re.sub(r"\s+(?:район|р-н)$", "", value).strip(" .,;:")
    return DISTRICTS.get(value)


def area_profile(area):
    if type(area) not in (int, float) or not math.isfinite(area):
        return None
    for i, (low, high) in enumerate(zip(AREA_EDGES, AREA_EDGES[1:])):
        if low <= area < high or i == len(AREA_EDGES)-2 and area == high:
            return {"id": f"a{low}_{high}", "lower_m2": low, "upper_m2": high,
                    "lower_inclusive": True, "upper_inclusive": high == AREA_EDGES[-1],
                    "reference_m2": (low+high)/2, "maximum_deviation_pct": round(100*(high-low)/(high+low), 4)}
    return None


def condition_claim(title, description):
    text = f"{title or ''} {description or ''}"[:10_000]
    # A negated/ambiguous declaration cannot establish comparable condition.
    if re.search(r"\b(?:не|без)\s+(?:евро|дизайнерск|косметическ)|требу\w*\s+ремонт|ремонт\w*\s+(?:нет|отсутствует)", text, re.I):
        return "unknown"
    matches = [key for key, pattern in CONDITIONS.items() if re.search(pattern, text, re.I)]
    return matches[0] if len(matches) == 1 else "unknown"


def _source_url(row):
    try:
        value = row.get("url")
        p = urlsplit(value) if isinstance(value, str) and len(value) <= 500 else None
        return bool(p and p.scheme == "https" and p.hostname in {"krisha.kz", "www.krisha.kz"}
                    and p.username is None and p.password is None and p.port is None
                    and not p.query and not p.fragment and re.fullmatch(r"[0-9]{5,14}", str(row.get("id", "")))
                    and p.path.rstrip("/") == f"/a/show/{row['id']}")
    except ValueError:
        return False


def _catalog_label(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 120
            or re.search(r"https?://|www\.|@|<|>|телефон|whatsapp|ватсап", value, re.I)
            or re.search(r"(?:\d[\s()\-]*){10}", value)):
        return None
    return value.strip()


def _json_number(value):
    return value if type(value) is int or type(value) is float and math.isfinite(value) else None


def rental_rows_sql():
    """All active Astana apartments, even stale/daily/unknown/flagged rows.

    Reuse the existing recursive source/canonical map. Eligibility is applied
    in memory so audit/exclusion denominators share the complete snapshot.
    """
    return rental_identity_ctes_sql(materialize_roots=True) + """
SELECT r.id,r.url,r.title,r.description,r.description_checked_at,
 r.city,r.prop_type,r.is_active,r.archived_at,r.rent_period,r.rent_period_conflict,
 r.price,r.area,r.rooms,r.floor,r.floors_total,r.address,r.district,
 r.complex_id,r.complex_name,r.complex_url,complex_name_key(r.complex_name) AS identity_raw_name_key,
 r.is_duplicate,r.dup_needs_review,r.duplicate_of,r.last_seen,
 r.furnishing,r.furnishing_version,r.rental_episode_started_at,r.rental_episode_last_seen_at,
 bound.cid AS bound_canonical_id,named.cid AS named_canonical_id,source.cid AS source_canonical_id
FROM rental_listings r
LEFT JOIN rental_canonical_map bound ON bound.id=r.complex_id
LEFT JOIN name_keys named ON r.complex_id IS NULL AND named.n=complex_name_key(r.complex_name)
LEFT JOIN rental_source_keys source ON r.complex_url ~ '^https://krisha[.]kz/complex/show/[^/?#]+(/[^/?#]+)?/?$'
 AND source.url=rtrim(r.complex_url,'/')
WHERE r.city='astana' AND r.prop_type='apartment' AND r.is_active IS TRUE
ORDER BY r.id LIMIT $1
"""


EVIDENCE_READS = (
    ("complexes", """SELECT id,name,address,complex_name_key(name) AS name_key,canonical_id,canonical_reason,
        is_garbage,is_street,is_umbrella,parent_complex_id,year_built FROM complexes ORDER BY id LIMIT $1""", 20_000),
    ("aliases", "SELECT complex_id,normalized_name,status FROM complex_aliases WHERE status='verified' ORDER BY complex_id,normalized_name LIMIT $1", 100_000),
    ("complex_reviews", "SELECT complex_id_a,complex_id_b FROM complex_duplicate_candidates WHERE status='review' ORDER BY complex_id_a,complex_id_b LIMIT $1", 100_000),
    ("relations", """SELECT complex_id_a,complex_id_b FROM complex_relations WHERE relation_type IN
        ('sibling_phase','same_umbrella_project','separate_neighbor_complex') ORDER BY complex_id_a,complex_id_b LIMIT $1""", 100_000),
    ("source_reviews", """SELECT complex_id,conflict_with_complex_id,source,url,kind FROM complex_source_link_candidates
        WHERE kind IN ('review','conflict') ORDER BY id LIMIT $1""", 100_000),
)


def _identity_rows(rows, evidence):
    """Stored catalog evidence only; never claim a fresh browser verification."""
    catalog = {r["id"]: r for r in evidence["complexes"]}
    children = defaultdict(list)
    for item in catalog.values():
        if (item.get("canonical_id") is None and item.get("parent_complex_id")
                and not item.get("is_garbage") and not item.get("is_street")):
            children[item["parent_complex_id"]].append(item)
    def root(identifier):
        seen = set()
        while identifier in catalog and identifier not in seen:
            seen.add(identifier)
            target = catalog[identifier].get("canonical_id")
            if target is None: return identifier
            identifier = target
        return None
    adapted = []
    for raw in rows:
        row = dict(raw)
        row["source_has_complex_claim"] = bool(row.get("complex_id") or row.get("complex_name") or row.get("complex_url"))
        cid = row.get("bound_canonical_id") if row.get("complex_id") is not None else row.get("named_canonical_id")
        c = catalog.get(cid, {})
        # Existing child FK + one exact matching child address, no geo inference.
        siblings = children.get(c.get("parent_complex_id"), [])
        matching = [item["id"] for item in siblings if _same_house_address(row.get("address"), item.get("address"))]
        exact_child = bool(row.get("complex_id") is not None and c.get("parent_complex_id")
                           and matching == [cid])
        row.update(complex_id=cid, complex_resolution="url" if cid else None,
                   resolved_house_id=cid if exact_child else None,
                   house_attribution="address" if exact_child else None)
        adapted.append(row)
    annotated = annotate_daily_pick_identity_rows(adapted, complexes=evidence["complexes"],
        aliases=evidence["aliases"], complex_reviews=evidence["complex_reviews"],
        relations=evidence["relations"], property_reviews=[])
    pending = set()
    # Deliberately conservative: a pending source candidate touching a family
    # vetoes that family; an unverified alternative is never silently ignored.
    for item in evidence["source_reviews"]:
        pending.update((root(item.get("complex_id")), root(item.get("conflict_with_complex_id"))))
    for row in annotated:
        family = {row.get("identity_complex_id"), row.get("identity_complex_parent_id")}-{None}
        row["identity_source_review_pending"] = bool(family & pending)
    return annotated


def _claims(row, now):
    """Only explicit fresh text; a catalog revisit cannot refresh old detail."""
    observed = utc(row.get("last_seen"))
    detail = utc(row.get("description_checked_at"))
    fresh_detail = bool(detail and now-timedelta(hours=72) <= detail <= now)
    condition = condition_claim(row.get("title"), row.get("description") if fresh_detail else None)
    furnished = classify_furnishing(row.get("title"), row.get("description") if fresh_detail else None)
    claimed = condition != "unknown" or furnished != "unknown"
    claim_time = min(observed, detail) if claimed and observed and fresh_detail else observed if claimed else None
    return condition, furnished, claim_time


def _reasons(row, now):
    reasons = []
    if row.get("rent_period") != "monthly": reasons.append("not_source_monthly")
    if row.get("rent_period_conflict") is not False: reasons.append("period_conflict_or_unknown_flag")
    if row.get("is_active") is not True or row.get("archived_at") is not None: reasons.append("not_active_unarchived")
    if row.get("is_duplicate") is True or row.get("duplicate_of"): reasons.append("known_duplicate")
    if row.get("dup_needs_review") is True: reasons.append("duplicate_review_pending")
    observed = utc(row.get("last_seen"))
    if observed is None or not now-timedelta(hours=TTL_HOURS) <= observed <= now: reasons.append("stale_missing_or_future_observation")
    if not _source_url(row): reasons.append("invalid_source_identity")
    price, area, rooms = (row.get(k) for k in ("price", "area", "rooms"))
    if type(price) is not int or not 50_000 <= price <= 2_000_000: reasons.append("invalid_monthly_price")
    if not area_profile(area): reasons.append("invalid_area")
    if type(rooms) is not int or not 1 <= rooms <= 6: reasons.append("invalid_rooms")
    if _NONMONTHLY.search(row.get("title") or "") or classify_rental_period(title=row.get("title") or "", verified_catalog_period="monthly") != "monthly":
        reasons.append("contrary_period_text")
    if not _single_source_house(row.get("address")): reasons.append("missing_or_ambiguous_house_address")
    d = district(row.get("district"))
    if d is None: reasons.append("missing_or_unknown_district")
    address_d = district((row.get("address") or "").split(",", 1)[0])
    if d and address_d and d != address_d: reasons.append("district_address_conflict")
    has_complex = row.get("source_has_complex_claim") is True
    if has_complex:
        cid = row.get("identity_complex_id")
        if not cid or row.get("identity_complex_valid") is not True: reasons.append("unresolved_catalog_identity")
        if row.get("identity_complex_is_umbrella") is True: reasons.append("unresolved_umbrella")
        if row.get("identity_name_ambiguous") is True: reasons.append("ambiguous_complex_name")
        if row.get("identity_source_name_conflict") is not False: reasons.append("stored_source_name_conflict")
        allowed = {cid}
        if row.get("resolved_house_id") and row.get("house_attribution") == "address":
            allowed.add(row.get("identity_complex_parent_id"))
        if row.get("source_canonical_id") not in allowed-{None}: reasons.append("source_catalog_link_conflict_or_missing")
    if row.get("identity_complex_review_pending") is not False: reasons.append("complex_duplicate_review_pending")
    if row.get("identity_source_review_pending") is not False: reasons.append("complex_source_review_pending")
    return sorted(set(reasons))


def _quantiles(values):
    values = sorted(values)
    if len(values) == 1:
        q25 = q75 = values[0]
    else:
        q25, _, q75 = statistics.quantiles(values, n=4, method="inclusive")
    median = statistics.median(values)
    return {"p25": round(q25, 4), "median": round(median, 4), "p75": round(q75, 4),
            "min": round(values[0], 4), "max": round(values[-1], 4),
            "relative_iqr": round((q75-q25)/median, 6)}


def build_slice(rows, evidence, settings, *, now, expected_count=None, max_scan=MAX_SCAN):
    now = utc(now)
    if now is None: raise ValueError("Timezone-aware snapshot time required")
    if type(max_scan) is not int or not 1 <= max_scan <= MAX_SCAN: raise ValueError("Invalid scan bound")
    if len(rows) > max_scan or type(expected_count) is not int or expected_count != len(rows):
        raise ValueError("Incomplete bounded rental population; no truncated export allowed")
    if any(r.get("city") != "astana" or r.get("prop_type") != "apartment" or r.get("is_active") is not True for r in rows):
        raise ValueError("Complete scan contains a row outside its declared scope")
    ids = [str(r.get("id", "")) for r in rows]
    if len(set(ids)) != len(ids): raise ValueError("Repeated source ID in complete scan")
    for name, _, maximum in EVIDENCE_READS:
        if name not in evidence or len(evidence[name]) > maximum: raise ValueError("Incomplete identity evidence")
    annotated = _identity_rows(rows, evidence)
    inventory = Counter(r.get("rent_period", "unknown") for r in rows)
    monthly = [r for r in annotated if r.get("rent_period") == "monthly"]
    audit = Counter()
    eligible = []
    private = []
    for row in monthly:
        observed = utc(row.get("last_seen"))
        audit["active_monthly_rows"] += 1
        for key, condition in (
            ("fresh72", bool(observed and now-timedelta(hours=72) <= observed <= now)),
            ("missing_address", not bool(row.get("address"))),
            ("missing_raw_complex_name", not bool(row.get("complex_name"))),
            ("missing_source_complex_url", not bool(row.get("complex_url"))),
            ("missing_bound_canonical_id", not bool(row.get("bound_canonical_id"))),
            ("known_duplicate", row.get("is_duplicate") is True or bool(row.get("duplicate_of"))),
            ("duplicate_review_pending", row.get("dup_needs_review") is True),
            ("period_conflict", row.get("rent_period_conflict") is True),
            ("unresolved_umbrella", row.get("identity_complex_is_umbrella") is True),
            ("complex_duplicate_review_pending", row.get("identity_complex_review_pending") is True),
            ("complex_source_review_pending", row.get("identity_source_review_pending") is True),
            ("stored_source_name_conflict", row.get("identity_source_name_conflict") is True),
            ("explicit_condition_claim", condition_claim(row.get("title"),row.get("description")) != "unknown"),
            ("explicit_furnishing_claim", classify_furnishing(row.get("title"),row.get("description")) != "unknown"),
        ):
            if condition: audit[key] += 1
        reasons = _reasons(row, now)
        profile = area_profile(row.get("area"))
        condition, furnishing, claim_at = _claims(row, now)
        if condition != "unknown": audit["fresh_explicit_condition_claim"] += 1
        if furnishing != "unknown": audit["fresh_explicit_furnishing_claim"] += 1
        d = district(row.get("district"))
        episode_first, episode_last = utc(row.get("rental_episode_started_at")), utc(row.get("rental_episode_last_seen_at"))
        episode_valid = bool(episode_first and episode_last and observed and episode_first <= episode_last <= observed
                             and now-timedelta(hours=72) <= episode_last <= now)
        safe = {"source_id": row["id"], "rent_period": "monthly", "price_monthly": _json_number(row.get("price")),
                "area_m2": _json_number(row.get("area")), "rooms": _json_number(row.get("rooms")), "floor": _json_number(row.get("floor")),
                "floors_total": _json_number(row.get("floors_total")), "district": d[0] if d else None,
                "district_label": d[1] if d else None, "area_profile": profile,
                "canonical_complex_id": row.get("identity_complex_id"),
                "canonical_complex_name": _catalog_label(row.get("identity_complex_name")),
                "house_id": row.get("resolved_house_id"), "parent_complex_id": row.get("identity_complex_parent_id"),
                "identity_status": "stored_catalog_source_agreement" if row.get("identity_complex_id") and not reasons else
                    "source_address_only" if not row.get("identity_complex_id") and not reasons else "excluded",
                "property_identity_status": "unavailable_for_rental_source_ids", "n_physical": None,
                "source_name_fresh_browser_verified": False,
                "condition_claim": condition, "furnishing_claim": furnishing,
                "condition_verified": False, "furnishing_verified": False,
                "claim_basis": "explicit_source_text_unvalidated_rules_not_photo_quality",
                "claims_observed_at": claim_at.isoformat() if claim_at else None,
                "last_seen": observed.isoformat() if observed else None, "continuous_episode_proof_valid": episode_valid,
                "exclusions": reasons}
        if not reasons:
            address_key = (_address_street_key(row.get("address")), _extract_house_number(row.get("address")))
            floor = row.get("floor")
            key = (*address_key, row["rooms"], round(row["area"], 1), floor) if type(floor) is int and 1<=floor<=100 else ("source", row["id"])
            safe["precautionary_group"] = hashlib.sha256(json.dumps(key,ensure_ascii=False).encode()).hexdigest()
            eligible.append(safe)
        private.append(safe)
    # The newest complete source record wins; no averages or mixed attributes.
    retained = {}
    for row in sorted(eligible, key=lambda r:(r["last_seen"], r["source_id"]), reverse=True):
        retained.setdefault(row["precautionary_group"], row)
    retained_ids = {row["source_id"] for row in retained.values()}
    for row in private:
        row["retained_after_precautionary_dedup"] = row["source_id"] in retained_ids
    groups, diagnostics = defaultdict(list), defaultdict(list)
    for row in eligible:
        scopes = [("district", row["district"], None)]
        if row["canonical_complex_id"]:
            scopes.append(("complex", row["canonical_complex_id"], row["canonical_complex_name"]))
        if row["house_id"]:
            scopes.append(("house", row["house_id"], row["canonical_complex_name"]))
        for scope, identifier, label in scopes:
            key = (scope,str(identifier),row["rooms"],row["area_profile"]["id"])
            diagnostics[key].append((row,label))
            groups[(*key,row["condition_claim"],row["furnishing_claim"])].append((row,label))
    def aggregate(key, members, stratified):
        before = [r for r, _ in members]
        values = [r for r in before if r["source_id"] in retained_ids]
        if not values: return None
        first = values[0]
        price = _quantiles([r["price_monthly"] for r in values])
        m2 = _quantiles([r["price_monthly"]/r["area_m2"] for r in values])
        minimum = 20 if key[0] == "district" else 10
        condition, furnishing = key[4:] if stratified else ("mixed_or_unknown", "mixed_or_unknown")
        blockers = []
        if len(values)<minimum: blockers.append("insufficient_source_observations")
        if max(price["relative_iqr"],m2["relative_iqr"])>.5: blockers.append("wide_distribution")
        if condition in {"unknown","mixed_or_unknown"}: blockers.append("condition_not_comparable")
        if furnishing in {"unknown","mixed_or_unknown"}: blockers.append("furnishing_not_comparable")
        if not stratified: blockers.append("unstratified_diagnostic_only")
        if key[0] != "district" and first["canonical_complex_name"] is None: blockers.append("missing_safe_catalog_label")
        times = sorted(r["last_seen"] for r in values)
        cid = first["canonical_complex_id"] if key[0] != "district" else None
        return {"cohort_id": ":".join(map(str,key)), "scope": key[0], "district": first["district"] if key[0]=="district" else None,
                "district_label": first["district_label"] if key[0]=="district" else None,
                "canonical_complex_id": cid, "complex_name": members[0][1] if cid else None,
                "house_id": first["house_id"] if key[0]=="house" else None,
                "rooms": first["rooms"], "area_profile": first["area_profile"],
                "condition_claim": condition,"furnishing_claim": furnishing,
                "condition_verified": False,"furnishing_verified": False,
                "n_source_ids_before_dedup": len(before),
                "n_unique_source_after_conservative_dedup": len(values),
                "precautionary_excess_ids_suppressed": len(before)-len(values),"n_physical": None,
                "property_identity_status": "unavailable_for_rental_source_ids",
                "monthly_asking_price_kzt": price,"monthly_asking_kzt_per_m2": m2,
                "oldest_observed_at": times[0],"newest_observed_at": times[-1],
                "source_age_hours_max": round((now-utc(times[0])).total_seconds()/3600,4),
                "description_claim_observed_at_oldest": min((r["claims_observed_at"] for r in values if r["claims_observed_at"]),default=None),
                "minimum_observations": minimum,"descriptive_ready": not blockers,
                "ranking_ready": False,"ranking_blockers": ["unverified_physical_independence","no_fresh_browser_peer_audit"],
                "descriptive_blockers": blockers,"claim_basis": "recent_explicit_source_text_unvalidated",
                "member_source_ids": sorted(r["source_id"] for r in values)}
    cohorts = [item for k in sorted(groups) if (item:=aggregate(k,groups[k],True))]
    profile_diagnostics = [item for k in sorted(diagnostics) if (item:=aggregate(k,diagnostics[k],False))]
    reasons = Counter(reason for row in private for reason in row["exclusions"])
    newest = max((utc(r.get("last_seen")) for r in monthly if utc(r.get("last_seen"))), default=None)
    return {"schema_version":1,"policy_version":VERSION,"generated_at":now.isoformat(),
            "complete_database_scan":True,"active_astana_apartment_rows_scanned":len(rows),
            "active_period_counts":dict(sorted(inventory.items())),"max_scan":max_scan,"truncated":False,
            "database_writes":0,"market_coverage":"unknown","scope":"observed_active_Astana_monthly_apartment_source_offers",
            "collector":collection_health(settings,newest,now=now),
            "audit":dict(sorted(audit.items())),"exclusions_nonexclusive":dict(sorted(reasons.items())),
            "eligible_monthly_source_ids_before_precautionary_dedup":len(eligible),
            "n_unique_source_after_conservative_dedup":len(retained),"n_physical":None,
            "property_identity_status":"unavailable_for_rental_source_ids",
            "dedup_method":"source_id_then_exact_street_house_rooms_area_0.1_floor_precautionary_newest_record",
            "dedup_is_verified_physical_identity":False,"precautionary_excess_ids_suppressed":len(eligible)-len(retained),
            "condition_basis":"explicit_recent_source_text_claim_not_verified_quality",
            "furnishing_classifier_validated":furnishing_validation_ready(settings),
            "furnishing_classifier_version":version_tag(settings.get("RENTAL_FURNISHING_RULES_JSON","{}")),
            "quantile_method":"inclusive_linear_interpolation","freshness_hours":72,
            "area_profiles":[area_profile(low) for low in AREA_EDGES[:-1]],
            "ready_descriptive_cohorts":sum(c["descriptive_ready"] for c in cohorts),
            "ranking_ready":False,"records":private,"cohorts":cohorts,"profile_diagnostics":profile_diagnostics,
            "limitations":["complete_database_scan_is_not_complete_market_coverage",
                "source_ids_and_precautionary_groups_are_not_verified_physical_apartments",
                "stored_complex_name_may_have_been_overwritten_no_fresh_browser_peer_proof",
                "asking_rent_is_not_signed_contract_rent_or_total_utilities_inclusive_payment",
                "description_claims_are_unvalidated_and_never_photo_quality",
                "absence_or_completed_crawl_does_not_prove_tenancy_or_demand",
                "no_ranking_recommendation_forecast_yield_or_time_to_let"]}
