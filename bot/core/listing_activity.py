"""Dated listing activity shared by market profiles and Property Timeline.

An old last_seen does not prove a listing was removed. Only dated archive
events end an episode. Archive intervals are half-open: active again at the
reactivation timestamp. A new ID is a confirmed relist only after all older
IDs of the property have been archived; overlapping IDs are not relists.
"""
from __future__ import annotations

from datetime import datetime


def archive_intervals(listing: dict, history: list[dict]) -> list[tuple[datetime, datetime | None]]:
    intervals = []
    for row in history:
        start, end = row.get("archived_at"), row.get("reactivated_at")
        if start is not None and end is not None and end > start:
            intervals.append((start, end))
    # archived_at is cleared on reactivation; a non-null value is a dated
    # current archive event even in legacy rows with an inconsistent flag.
    if listing.get("archived_at") is not None:
        intervals.append((listing["archived_at"], None))
    return sorted(intervals, key=lambda interval: interval[0])


def active_at(listing: dict, history: list[dict], at: datetime) -> bool:
    first_seen = listing.get("first_seen")
    if first_seen is None or first_seen > at:
        return False
    return not any(start <= at and (end is None or at < end)
                   for start, end in archive_intervals(listing, history))


def active_intervals(listing: dict, history: list[dict], as_of: datetime) -> list[tuple[datetime, datetime]]:
    """Observed activity through as_of, excluding every known archive gap."""
    cursor = listing.get("first_seen")
    if cursor is None or cursor > as_of:
        return []
    result = []
    for start, end in archive_intervals(listing, history):
        if start > as_of:
            break
        if end is not None and end <= cursor:
            continue
        if start > cursor:
            result.append((cursor, start))
        if end is None or end > as_of:
            return result
        cursor = max(cursor, end)
    if cursor <= as_of:
        result.append((cursor, as_of))
    return result


def merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if end < start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def confirmed_relists(listings: list[dict], histories: dict[str, list[dict]]) -> dict[str, str]:
    """new listing_id -> previous listing_id; same-ID returns are reactivations.

    Equal-time new IDs are processed deterministically, so simultaneous ads
    for the same new episode do not inflate the relist count.
    """
    previous: list[dict] = []
    result: dict[str, str] = {}
    ordered = sorted((row for row in listings if row.get("first_seen") is not None),
                     key=lambda row: (row["first_seen"], row["listing_id"]))
    for row in ordered:
        at = row["first_seen"]
        if previous and all(not active_at(old, histories.get(old["listing_id"], []), at)
                            for old in previous):
            result[row["listing_id"]] = previous[-1]["listing_id"]
        previous.append(row)
    return result
