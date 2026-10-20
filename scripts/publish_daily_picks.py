#!/usr/bin/env python3
"""Read-only daily selection and atomic public snapshot publisher.

No application pool or migrations are initialized. The connection and
repeatable-read transaction both enforce read-only PostgreSQL access.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.core.daily_picks import (
    LOCAL_TIMEZONE, MAX_PICKS, build_daily_picks, load_daily_picks,
    publish_snapshot, safe_photo_url, photo_asset_key, snapshot_path, validate_snapshot,
)
from bot.core.daily_pick_identity import annotate_daily_pick_identity_rows, matching_source_complex_name
from bot.core.apartment_selection_reasons import best_apartment_factors, validate_selection_facts

DEFAULT_QUALITY_POLICY = "discounted_four_factors"
MINIMUM_PICKS = 10

# Restricted input fields. The source address is sanitized to street/house by
# the public projection. Seller contacts, documents, descriptions,
# administrative notes and application credentials are never selected.
CANDIDATES_SQL = """
WITH photos AS (
    SELECT f.listing_id, jsonb_agg(f.photo_url ORDER BY f.id) AS approved_photo_urls
    FROM listing_photo_fingerprints f
    JOIN apartment_listings p ON p.id = f.listing_id
    WHERE f.fetch_status = 'ok' AND f.photo_type = 'interior'
      AND p.is_active IS TRUE AND p.last_seen BETWEEN $1::timestamptz - interval '72 hours' AND $1
      AND p.photos @> jsonb_build_array(f.photo_url)
    GROUP BY f.listing_id
), history AS (
    SELECT listing_id, jsonb_agg(jsonb_build_object(
        'old_price', old_price, 'new_price', new_price, 'changed_at', changed_at)
        ORDER BY changed_at,id) AS price_events
    FROM price_history
    WHERE changed_at <= $1
    GROUP BY listing_id
)
SELECT a.id, a.url, a.price, a.area, a.rooms, a.floor, a.floors_total, a.district, a.address,
       a.year_built, a.is_owner, a.is_active, a.is_duplicate, a.dup_needs_review,
       a.market_type, a.last_seen, a.resolved_house_id, a.house_attribution,
       a.complex_id, a.complex_resolution, a.lat, a.lon, a.finish_level,
       a.ceiling_height, a.kitchen_area, pl.property_id,a.photos AS candidate_photo_urls,
       c.name AS canonical_complex_name, c.year_built AS complex_year_built,
       complex_name_key(a.complex_name) AS identity_raw_name_key,
       p.approved_photo_urls, coalesce(h.price_events,'[]'::jsonb) AS price_events
FROM apartment_listings a
LEFT JOIN property_listings pl ON pl.listing_id = a.id
LEFT JOIN complexes c ON c.id=a.complex_id
LEFT JOIN photos p ON p.listing_id = a.id
LEFT JOIN history h ON h.listing_id = a.id
WHERE a.is_active IS TRUE AND COALESCE(a.is_duplicate, FALSE) = FALSE
  AND COALESCE(a.dup_needs_review, FALSE) = FALSE AND a.market_type = 'secondary'
  AND a.last_seen BETWEEN $1::timestamptz - interval '72 hours' AND $1
  AND a.price > 0 AND a.area > 0
ORDER BY a.id
"""

# Separate bounded scans avoid PostgreSQL inlining catalog/EXISTS evidence
# into the 20k-listing query. All six reads share the same read-only snapshot.
IDENTITY_CATALOG_SQL = """
SELECT id,name,complex_name_key(name) AS name_key,canonical_id,canonical_reason,
       is_garbage,is_street,is_umbrella,parent_complex_id,year_built
FROM complexes ORDER BY id LIMIT $1
"""
IDENTITY_ALIASES_SQL = """
SELECT complex_id,normalized_name,status FROM complex_aliases WHERE status='verified' LIMIT $1
"""
IDENTITY_COMPLEX_REVIEWS_SQL = """
SELECT complex_id_a,complex_id_b FROM complex_duplicate_candidates WHERE status='review' LIMIT $1
"""
IDENTITY_RELATIONS_SQL = """
SELECT complex_id_a,complex_id_b FROM complex_relations
WHERE relation_type IN ('sibling_phase','same_umbrella_project','separate_neighbor_complex') LIMIT $1
"""
IDENTITY_PROPERTY_REVIEWS_SQL = """
SELECT review.listing_id,review.candidate_property_id,source.property_id AS source_property_id
FROM property_match_candidates review LEFT JOIN property_listings source ON source.listing_id=review.listing_id
WHERE review.status='pending' LIMIT $1
"""
IDENTITY_READS = (
    ("complexes", IDENTITY_CATALOG_SQL, 20_000),
    ("aliases", IDENTITY_ALIASES_SQL, 100_000),
    ("complex_reviews", IDENTITY_COMPLEX_REVIEWS_SQL, 100_000),
    ("relations", IDENTITY_RELATIONS_SQL, 100_000),
    ("property_reviews", IDENTITY_PROPERTY_REVIEWS_SQL, 500_000),
)


async def read_candidates(dsn: str, now: datetime) -> list[dict]:
    import asyncpg
    connection = await asyncpg.connect(
        dsn, command_timeout=60,
        server_settings={"default_transaction_read_only": "on", "application_name": "hatuli_daily_picks_readonly"},
    )
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            rows = [dict(row) for row in await connection.fetch(CANDIDATES_SQL, now)]
            evidence = {}
            for name, query, maximum in IDENTITY_READS:
                records = [dict(row) for row in await connection.fetch(query, maximum + 1)]
                if len(records) > maximum:
                    raise ValueError(f"Identity evidence exceeds the bounded {name} scan")
                evidence[name] = records
            return annotate_daily_pick_identity_rows(rows, **evidence)
    finally:
        await connection.close()


async def verify_photos(snapshot: dict, deadline: float | None = None) -> tuple[dict, int]:
    """HEAD only: no image download, redirect, parser, or anti-bot bypass."""
    import httpx
    checked = 0
    valid_items = []
    async with httpx.AsyncClient(timeout=3, follow_redirects=False, headers={"User-Agent": "HatuliDailyPicks/1.0"}) as client:
        for item in snapshot["items"]:
            valid = []
            for url in item["photo_urls"]:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("Photo verification budget exhausted")
                if safe_photo_url(url) is None:
                    continue
                try:
                    response = await client.head(url)
                    checked += 1
                    if response.status_code == 200 and response.headers.get("content-type", "").lower().startswith("image/"):
                        valid.append(url)
                except httpx.HTTPError:
                    checked += 1
                await asyncio.sleep(.12)
            if valid:
                valid_items.append({**item, "photo_urls": valid})
    snapshot = {**snapshot, "items": valid_items}
    for rank, item in enumerate(valid_items, 1):
        item["rank"] = rank
    return snapshot, checked


class SourceBlockedError(RuntimeError):
    """Stop immediately; no retry, header rotation, or alternate transport."""
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Source access blocked with HTTP {status}")


def parse_source_offer(html: str, item: dict) -> str:
    """Only an identified main offer can confirm availability and current ask."""
    from bs4 import BeautifulSoup
    if not isinstance(html, str) or len(html) > 2_000_000:
        return "unknown"
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.select_one('link[rel="canonical"]')
    og_url = soup.select_one('meta[property="og:url"]')
    urls = [tag.get(attr, "") for tag, attr in ((canonical, "href"), (og_url, "content")) if tag]
    if not urls or any(url.rstrip("/") != item["source_url"] for url in urls):
        return "unknown"
    offer = soup.select_one(".offer") or soup.select_one("main")
    title_el = (offer.select_one("h1") or offer.select_one(".offer__title")) if offer else None
    price_el = offer.select_one(".offer__price") if offer else None
    if not offer or not title_el or not price_el:
        return "unknown"
    text = offer.get_text(" ", strip=True)
    if re.search(r"в архиве|может быть неактуальным|объявление (?:не найдено|удалено)", text, re.I):
        return "archived"
    title = title_el.get_text(" ", strip=True)
    price_text = price_el.get_text(" ", strip=True)
    if re.search(r"\bот\b", price_text, re.I) or not re.search(r"₸|тг|тенге", price_text, re.I):
        return "unknown"
    room_match = re.search(r"(\d+)[-\s]*комнат", title, re.I)
    area_match = re.search(r"(\d+(?:[.,]\d+)?)\s*м(?:²|2)", title, re.I)
    price_digits = re.sub(r"\D", "", price_text)
    if not room_match or not area_match or not price_digits or "квартир" not in title.casefold():
        return "unknown"
    breadcrumbs = soup.select('[itemtype*="BreadcrumbList"] a, .breadcrumbs a, .bread-crumbs a')
    sale_astana = False
    sale_category, astana_city = False, False
    for anchor in breadcrumbs:
        try:
            parsed = urlsplit(anchor.get("href", ""))
            if parsed.path.rstrip("/") == "/prodazha/kvartiry/astana" and (not parsed.hostname or parsed.hostname in {"krisha.kz", "www.krisha.kz"}):
                sale_astana = True
            if not parsed.hostname or parsed.hostname in {"krisha.kz", "www.krisha.kz"}:
                sale_category |= parsed.path.rstrip("/") == "/prodazha/kvartiry"
                astana_city |= parsed.path.rstrip("/") == "/catalog/astana"
        except ValueError:
            continue
    if not sale_astana and not (sale_category and astana_city):
        return "unknown"
    if int(price_digits) != item["price"] or int(room_match[1]) != item["rooms"] or abs(float(area_match[1].replace(",", ".")) - item["area"]) > .2:
        return "mismatch"
    floor_match = re.search(r"(\d+)\s*/\s*(\d+)\s*этаж", title, re.I)
    if floor_match and (int(floor_match[1]) != item["floor"] or int(floor_match[2]) != item["floors_total"]):
        return "mismatch"
    return "active"


async def verify_sources(snapshot: dict, deadline: float | None = None) -> tuple[dict, int, set[str]]:
    """Bounded ordinary GETs; only positively confirmed sources are retained."""
    import httpx
    checked = 0
    confirmed = []
    invalid_ids = set()
    async with httpx.AsyncClient(timeout=8, follow_redirects=False, headers={"User-Agent": "HatuliDailyPicks/1.0", "Accept-Language": "ru-RU,ru;q=0.9"}) as client:
        for item in snapshot["items"]:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Source verification budget exhausted")
            try:
                response = await client.get(item["source_url"])
                checked += 1
                if response.status_code in (403, 429, 468):
                    raise SourceBlockedError(response.status_code)
                if response.status_code in (404, 410):
                    invalid_ids.add(item["id"])
                elif response.status_code == 200 and response.headers.get("content-type", "").split(";", 1)[0].strip().lower() in {"text/html", "application/xhtml+xml"}:
                    state = parse_source_offer(response.text, item)
                    if state == "active":
                        confirmed.append({**item, "source_status": "active", "source_checked_at": datetime.now(timezone.utc).isoformat()})
                    elif state in {"archived", "mismatch"}:
                        invalid_ids.add(item["id"])
            except httpx.HTTPError:
                checked += 1
            await asyncio.sleep(1.5)
    return {**snapshot, "items": confirmed}, checked, invalid_ids


def load_source_checks(path: str, now: datetime) -> dict[str, dict]:
    """Owner-created evidence from normal browser visits; not an end-user API."""
    from datetime import timedelta
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("method") != "normal_browser" or not isinstance(payload.get("items"), list) or len(payload["items"]) > 100:
        raise ValueError("Invalid source confirmation file")
    result = {}
    for record in payload["items"]:
        if not isinstance(record, dict) or record.get("status") not in {"active", "archived", "deleted", "mismatch", "unknown"}:
            raise ValueError("Invalid browser confirmation")
        lid = record.get("id")
        if not isinstance(lid, str) or not re.fullmatch(r"[0-9]{5,20}", lid) or record.get("source_url") != f"https://krisha.kz/a/show/{lid}" or lid in result:
            raise ValueError("Invalid browser source identity")
        checked_at = datetime.fromisoformat(record["checked_at"])
        if checked_at.tzinfo is None or not now - timedelta(hours=2) <= checked_at <= now:
            raise ValueError("Stale browser source confirmation")
        result[lid] = record
    return result


def apply_source_checks(snapshot: dict, checks: dict[str, dict]) -> tuple[dict, int, set[str]]:
    confirmed, invalid = [], set()
    for item in snapshot["items"]:
        record = checks.get(item["id"])
        if not record:
            continue
        if record["status"] in {"archived", "deleted", "mismatch"}:
            invalid.add(item["id"])
        elif record["status"] == "active":
            if record.get("sale_astana") is not True or type(record.get("price")) is not int or record["price"] != item["price"] or type(record.get("rooms")) is not int or record["rooms"] != item["rooms"] or type(record.get("area")) not in {int, float} or not math.isfinite(record["area"]) or abs(record["area"] - item["area"]) > .2:
                invalid.add(item["id"])
                continue
            if any(field in record and record[field] != item[field] for field in ("floor", "floors_total")):
                invalid.add(item["id"])
                continue
            if "is_pledged" in record and type(record["is_pledged"]) is not bool:
                raise ValueError("Invalid browser pledge observation")
            source_facts = {"is_pledged": True} if record.get("is_pledged") is True else {}
            confirmed.append({**item, "source_status": "active", "source_checked_at": record["checked_at"],
                              "source_facts": source_facts})
    return {**snapshot, "items": confirmed}, 0, invalid


def matching_browser_offer(row: dict, record) -> bool:
    """Match current basic source facts before accepting optional attributes."""
    if not isinstance(record, dict):
        return False
    lid = str(row.get("id"))
    expected_url = f"https://krisha.kz/a/show/{lid}"
    area = record.get("area")
    try:
        area_matches = (type(area) in {int, float} and math.isfinite(area)
                        and abs(float(row["area"]) - area) <= .2)
    except (ValueError, TypeError, KeyError, OverflowError):
        area_matches = False
    return bool(
        record.get("id") == lid and record.get("source_url") == expected_url
        and row.get("url") == expected_url and record.get("status") == "active"
        and record.get("sale_astana") is True
        and type(record.get("price")) is int and record["price"] == row.get("price")
        and type(record.get("rooms")) is int and record["rooms"] == row.get("rooms")
        and area_matches
        and all(type(record.get(field)) is int and record[field] == row.get(field)
                for field in ("floor", "floors_total"))
    )


def source_identity_rows(rows: list[dict], checks: dict[str, dict]) -> list[dict]:
    """Exclude observed conflicts from both ranking and the comparable pool.

    Unvisited peers retain the catalog/raw-source consistency gate; they are
    not claimed to have received today's browser verification.
    """
    return [row for row in rows if str(row.get("id")) not in checks or (
        matching_browser_offer(row, checks[str(row["id"])])
        and matching_source_complex_name(row, checks[str(row["id"])]))]


def browser_measurements(record: dict, area) -> dict:
    """Only two optional numeric facts; missing/null remains unknown."""
    measurements = {field: record[field] for field in ("kitchen_area", "ceiling_height")
                    if record.get(field) is not None}
    safe = validate_selection_facts(measurements, area)
    return {field: safe[field] for field in measurements}


def owner_evidence_rows(rows: list[dict], checks: dict[str, dict] | None = None, *, review_preview: bool = False) -> list[dict]:
    """Owner/measurements require a matching normal-browser observation.

    Private candidate export may show the provisional benefit so an owner can
    inspect it. That export is explicitly unverified and cannot be published.
    The real publisher always overwrites any incoming marker with fresh proof.
    """
    result = []
    for row in rows:
        record = (checks or {}).get(str(row.get("id")))
        matches = matching_browser_offer(row, record)
        verified = row.get("is_owner") is True and (review_preview or matches and record.get("is_owner") is True)
        measurements = browser_measurements(record, row.get("area")) if matches and not review_preview else {}
        result.append({**row, **measurements, "owner_source_verified": verified})
    return result


def _safe_photo_list(value) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(url for raw in value if (url := safe_photo_url(raw))))


def photo_evidence_rows(rows: list[dict], checks: dict[str, dict] | None, now: datetime, *, review_preview: bool = False) -> list[dict]:
    """Raw photos are discovery only; publication requires human visual proof.

    Every publication photo requires an owner-created receipt tied to the
    same fresh source visit and actual URLs seen in the current listing.
    Cached fingerprint approvals are discovery hints, not today's visual
    inspection. A known CDN/format derivative
    must match the same stored UUID and frame index; no URL is constructed.
    Nothing is written back to PostgreSQL.
    """
    from datetime import timedelta
    result = []
    for row in rows:
        current = _safe_photo_list(row.get("candidate_photo_urls"))
        approved = _safe_photo_list(row.get("approved_photo_urls"))
        if "candidate_photo_urls" in row:
            approved = [url for url in approved if url in current]
        if review_preview:
            approved.extend(url for url in current if url not in approved)
        else:
            # Keep the row in the comparable population, but never let an old
            # fingerprint approval substitute for this visit's visual proof.
            approved = []
            record = (checks or {}).get(str(row.get("id")))
            if isinstance(record, dict) and (record.get("interior_photo_urls") is not None or record.get("photo_review") is not None):
                receipt, interiors = record.get("photo_review"), record.get("interior_photo_urls")
                if (not matching_browser_offer(row, record) or not isinstance(receipt, dict)
                        or receipt.get("method") != "normal_browser_visual"
                        or receipt.get("photo_type") not in (None, "interior")
                        or receipt.get("checked_at") != record.get("checked_at")
                        or not isinstance(interiors, list) or not 1 <= len(interiors) <= 3):
                    raise ValueError("Invalid source-linked interior photo receipt")
                try:
                    checked = datetime.fromisoformat(record["checked_at"].replace("Z", "+00:00"))
                    fresh = checked.tzinfo is not None and now - timedelta(hours=2) <= checked <= now
                except (ValueError, TypeError, KeyError, AttributeError):
                    fresh = False
                current_assets = {photo_asset_key(url) for url in current}
                reviewed_assets = [photo_asset_key(url) for url in interiors]
                if (not fresh or None in reviewed_assets or len(set(reviewed_assets)) != len(reviewed_assets)
                        or any(key not in current_assets for key in reviewed_assets)):
                    raise ValueError("Interior photo proof is stale or not part of the current source listing")
                # A fresh visual receipt replaces the entire photo selection.
                # Older fingerprint approvals must neither add unseen frames
                # nor displace today's actually inspected interior URLs.
                approved = list(interiors)
        result.append({**row, "approved_photo_urls": list(dict.fromkeys(approved))[:3]})
    return result


def preview_with_recent_measurements(rows: list[dict], snapshot: dict | None, now: datetime) -> list[dict]:
    """Private discovery hints only; real generation never calls this helper.

    A previously verified number may fill a missing DB field so the browser
    opens the offer again. The old number is never a new source confirmation.
    """
    from datetime import timedelta
    previous = {item["id"]: item for item in (snapshot or {}).get("items", [])}
    result = []
    for row in rows:
        safe = dict(row)
        item = previous.get(str(row.get("id")))
        if isinstance(item, dict) and item.get("source_status") == "active":
            try:
                checked = datetime.fromisoformat(item["source_checked_at"].replace("Z", "+00:00"))
                recent = checked.tzinfo is not None and now - timedelta(hours=72) <= checked <= now
            except (ValueError, TypeError, KeyError, AttributeError):
                recent = False
            record = {**item, "status": "active", "sale_astana": True}
            if recent and matching_browser_offer(row, record):
                measurements = browser_measurements(item.get("selection_facts") or {}, row.get("area"))
                safe.update({field: value for field, value in measurements.items() if row.get(field) is None})
        result.append(safe)
    return result


async def export_candidate_pool(target: str, now: datetime) -> dict:
    """Private review candidates, never a source-verified public snapshot."""
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "krisha_bot" / ".env")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is required")
    rows = owner_evidence_rows(await read_candidates(dsn, now), review_preview=True)
    rows = photo_evidence_rows(rows, None, now, review_preview=True)
    rows = preview_with_recent_measurements(rows, load_daily_picks(), now)
    excluded = set()
    items = []
    for _ in range(3):
        batch = build_daily_picks(rows, now, excluded_ids=excluded, minimum_factor_count=1)
        if not batch["items"]:
            break
        items.extend(batch["items"])
        excluded.update(item["id"] for item in batch["items"])
    review_photos = {str(row["id"]): _safe_photo_list(row.get("candidate_photo_urls")) for row in rows}
    for rank, item in enumerate(items, 1):
        item["rank"] = rank
        # Private discovery only. Public cards keep the independent 1-3-photo
        # allowlist and never inherit this unclassified current gallery.
        item["review_photo_urls"] = review_photos.get(item["id"], [])
    result = {"schema_version": 1, "kind": "unverified_review_candidates", "generated_at": now.isoformat(), "input_count": len(rows), "items": items}
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    if target == "-":
        print(encoded)
    else:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        import tempfile
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return result


@contextmanager
def publisher_lock(path: Path):
    """Nonblocking, process-scoped Linux lock; web workers never acquire it."""
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def generate(args, now: datetime) -> dict:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "krisha_bot" / ".env")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is required")
    source_checks = load_source_checks(args.source_checks, now) if getattr(args, "source_checks", None) else None
    if source_checks is None and not args.skip_photo_check:
        raise ValueError("Fresh normal-browser source confirmations are required")
    candidates = await read_candidates(dsn, now)
    candidates = source_identity_rows(candidates, source_checks) if source_checks is not None else candidates
    rows = owner_evidence_rows(candidates, source_checks)
    rows = photo_evidence_rows(rows, source_checks, now)
    snapshot = build_daily_picks(rows, now)
    head_count = 0
    source_gets = 0
    if not args.skip_photo_check:
        # A failed image removes its card. Refill from the next eligible item;
        # at most 90 candidates and three photos each, bounded sequential HEADs.
        attempted_ids = set()
        invalid_ids = {lid for lid, check in (source_checks or {}).items() if check["status"] in {"archived", "deleted", "mismatch"}}
        accepted = {}
        deadline = time.monotonic() + 420
        for _ in range(3):
            remaining = [row for row in rows if str(row["id"]) not in invalid_ids]
            batch = build_daily_picks(remaining, now, excluded_ids=attempted_ids)
            attempted_ids.update(item["id"] for item in batch["items"])
            if source_checks is None:
                batch, gets, newly_invalid = await verify_sources(batch, deadline)
            else:
                batch, gets, newly_invalid = apply_source_checks(batch, source_checks)
            source_gets += gets
            invalid_ids.update(newly_invalid)
            batch, count = await verify_photos(batch, deadline)
            head_count += count
            for item in batch["items"]:
                accepted[item["id"]] = item
            remaining = [row for row in rows if str(row["id"]) not in invalid_ids]
            excluded = {str(row["id"]) for row in remaining} - accepted.keys()
            refreshed = build_daily_picks(remaining, now, excluded_ids=excluded)
            if len(refreshed["items"]) >= MAX_PICKS:
                break
            # Exclude attempted cards, retaining every row in the comparable pool.
        snapshot = refreshed
        snapshot["items"] = [{**item, "photo_urls": accepted[item["id"]]["photo_urls"], "source_status": "active", "source_checked_at": accepted[item["id"]]["source_checked_at"],
                              "source_facts": accepted[item["id"]].get("source_facts", {})} for item in snapshot["items"]]
        finished_at = datetime.now(timezone.utc)
        snapshot["generated_at"] = finished_at.isoformat()
        snapshot["selection_date"] = finished_at.astimezone(LOCAL_TIMEZONE).date().isoformat()
        snapshot["source_observed_at"] = max((item["observed_at"] for item in snapshot["items"]), default=now.isoformat())
    return {"snapshot": snapshot, "input_count": len(rows), "photo_heads": head_count, "source_gets": source_gets}


def publication_satisfies_policy(snapshot: dict, quality_policy: str = DEFAULT_QUALITY_POLICY) -> bool:
    """An older three-factor edition never satisfies the current requirements."""
    if quality_policy not in {DEFAULT_QUALITY_POLICY, "daily_30"}:
        return False
    try:
        safe = validate_snapshot(snapshot)
        count = len(safe["items"])
        policy = safe.get("publication_policy") or {}
        return (MINIMUM_PICKS <= count <= MAX_PICKS and len(snapshot.get("items", [])) == count
                and (quality_policy != "daily_30" or count == MAX_PICKS)
                and policy.get("name") == DEFAULT_QUALITY_POLICY
                and policy.get("minimum_count") == MINIMUM_PICKS
                and policy.get("published_count") == count
                and policy.get("required_strong_factors") == 4
                and policy.get("minimum_discount_pct") == 3
                and policy.get("explicit") is True)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def prepare_publication(snapshot: dict, quality_policy: str = DEFAULT_QUALITY_POLICY, *, allow_partial: bool = False) -> dict:
    """Publish 10-30 discounted cards with four evidence-backed factors each."""
    count = len(snapshot.get("items", []))
    if quality_policy not in {"daily_30", DEFAULT_QUALITY_POLICY}:
        raise ValueError("Unknown daily quality policy")
    if allow_partial:
        raise ValueError("Partial publication cannot bypass the minimum of ten discounted four-factor cards")
    if not MINIMUM_PICKS <= count <= MAX_PICKS or quality_policy == "daily_30" and count != MAX_PICKS:
        raise RuntimeError(f"Only {count} qualifying cards; previous snapshot retained")
    safe = validate_snapshot(snapshot)
    generated = datetime.fromisoformat(safe["generated_at"].replace("Z", "+00:00"))
    if len(safe["items"]) != count:
        raise ValueError("Every quality-edition card must retain its verified evidence")
    for item in safe["items"]:
        factors = best_apartment_factors(item, generated)
        discount = item.get("price_comparison", {}).get("discount_pct")
        if (len(item["reasons"]) != 4 or len(factors) < 4 or item.get("complex_identity") is None
                or not any(factor["key"] == "price" for factor in factors[:4])
                or type(discount) not in {int, float} or not math.isfinite(discount) or discount < 3):
            raise ValueError("Every card requires identity, a discount of at least 3% and four proven factors")
    safe["publication_policy"] = {
        "name": DEFAULT_QUALITY_POLICY, "target_count": MAX_PICKS, "minimum_count": MINIMUM_PICKS,
        "published_count": count, "required_strong_factors": 4, "minimum_discount_pct": 3,
        "reason": "discount_and_four_factors", "explicit": True,
        "evaluated_at": safe["generated_at"],
    }
    return validate_snapshot(safe)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Read and evaluate; do not publish")
    parser.add_argument("--force", action="store_true", help="Explicitly republish today's snapshot")
    parser.add_argument("--skip-photo-check", action="store_true", help="Dry-run only; avoids CDN HEAD requests")
    parser.add_argument("--allow-partial", action="store_true", help="Retired: partial publication cannot bypass the minimum quality policy")
    parser.add_argument("--quality-policy", choices=(DEFAULT_QUALITY_POLICY, "daily_30"), default=DEFAULT_QUALITY_POLICY,
                        help="Default: 10-30 discounted cards with four factors; daily_30 additionally requires exactly 30")
    parser.add_argument("--source-checks", help="Fresh owner-created normal-browser confirmation JSON; missing confirmations fail closed")
    parser.add_argument("--export-candidates", metavar="PATH", help="Read-only candidate export, no HTTP requests. Use '-' for JSON stdout.")
    args = parser.parse_args()
    if args.skip_photo_check and not args.dry_run:
        parser.error("--skip-photo-check is only permitted with --dry-run")
    if args.allow_partial:
        parser.error("--allow-partial is retired; every publication requires at least ten discounted four-factor cards")
    now = datetime.now(timezone.utc)
    path = snapshot_path()
    try:
        if args.export_candidates:
            exported = asyncio.run(export_candidate_pool(args.export_candidates, now))
            if args.export_candidates != "-":
                print(json.dumps({"status": "candidates_exported", "count": len(exported["items"]), "input_count": exported["input_count"]}))
            return 0
        if args.dry_run:
            result = asyncio.run(generate(args, now))
        else:
            with publisher_lock(path):
                existing = load_daily_picks(path)
                if existing and existing["selection_date"] == now.astimezone(LOCAL_TIMEZONE).date().isoformat() and publication_satisfies_policy(existing, args.quality_policy) and not args.force:
                    print(json.dumps({"status": "already_published", "selection_date": existing["selection_date"], "count": len(existing["items"])}))
                    return 0
                result = asyncio.run(generate(args, now))
                snapshot = prepare_publication(result["snapshot"], args.quality_policy, allow_partial=args.allow_partial)
                result["snapshot"] = snapshot
                publish_snapshot(snapshot, path)
        snapshot = result["snapshot"]
        print(json.dumps({"status": "dry_run" if args.dry_run else "published", "selection_date": snapshot["selection_date"], "input_count": result["input_count"], "eligible_count": snapshot["total_candidates"], "count": len(snapshot["items"]), "photo_heads": result["photo_heads"], "source_gets": result["source_gets"], "quality_policy": args.quality_policy, "publication_policy": snapshot.get("publication_policy"), "ids": [item["id"] for item in snapshot["items"]]}, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Do not log DSNs or raw rows; server logs retain only the error class.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "quality_policy": args.quality_policy, "previous_snapshot_retained": True}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
