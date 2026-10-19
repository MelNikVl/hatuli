"""Pure, bounded eligibility and FIFO ordering for sale source details.

Catalog observations, legacy details_fetched and derived recalculation do not
establish source-detail freshness. Only the dedicated successful-source date
can make an unchanged listing fresh; a real failed attempt rotates the queue.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

DETAIL_MAX_AGE = timedelta(hours=72)
DETAIL_RETRY_AFTER = timedelta(hours=1)


def observed_time(value, now):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    value = value.astimezone(timezone.utc)
    return value if value <= now else None


def detail_due(row, stored, *, now):
    if row.get("details_fetched"):
        return False  # Already recognized in this in-memory batch.
    stored = stored or {}
    attempted = observed_time(stored.get("source_details_attempted_at"), now)
    if attempted is not None and now - attempted < DETAIL_RETRY_AFTER:
        return False
    checked = observed_time(stored.get("source_details_checked_at"), now)
    return (checked is None or now - checked >= DETAIL_MAX_AGE
            or stored.get("price") != row.get("price")
            or stored.get("lat") is None)


def detail_candidates(rows, known, *, now):
    epoch = datetime.min.replace(tzinfo=timezone.utc)

    def order(row):
        stored = known.get(row.get("id"), {})
        checked = observed_time(stored.get("source_details_checked_at"), now)
        attempted = observed_time(stored.get("source_details_attempted_at"), now)
        found = observed_time(stored.get("found_at"), now)
        # A failed request moves behind older work instead of pinning the
        # first unavailable row forever. Never infer successful freshness.
        return (max(checked or found or epoch, attempted or epoch), str(row.get("id", "")))

    return sorted((row for row in rows if detail_due(row, known.get(row.get("id")), now=now)), key=order)


def observed_detail_values(details):
    """A source patch, never a merged catalog row or inferred default flags."""
    fields = ("floor", "floors_total", "year_built", "building_type", "renovation",
              "furniture", "kitchen_area", "ceiling_height", "photos", "lat", "lon",
              "seller_type", "is_owner", "views_count")
    patch = {key: details[key] for key in fields if key in details and details[key] is not None}
    for source, target in (("area_total", "area"), ("address_full", "address")):
        if source in details and details[source] is not None:
            patch[target] = details[source]
    identity = details.get("listing_identity")
    if isinstance(identity, dict):
        for key in ("rooms", "area"):
            if identity.get(key) is not None:
                patch[key] = identity[key]
    description = details.get("description")
    if isinstance(description, str) and (description.strip() or details.get("description_observed") is True):
        patch["description"] = description
    # Preserve the actual source spelling/link, including an unverified name.
    # This is evidence for later identity guards, never a canonical FK guess.
    if (details.get("source_detail_valid") is True and not details.get("is_archived")
            and details.get("complex_source") == "official_map_block"):
        from bot.core.rental_stage0_identity import source_complex_url
        name = details.get("complex_name")
        url = source_complex_url(details.get("complex_url"))
        if isinstance(name, str) and name.strip() and url:
            patch.update(complex_name=name.strip(), complex_url=url)
    return patch
