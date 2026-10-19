"""Reviewed negative sale-source evidence; no I/O or positive verification.

A hold is conservative exclusion, not a physical merge or database repair.
It does not expire when a stored source name is overwritten or leaves a feed.
Removing one requires a separately reviewed correction of the conflict.
"""
from __future__ import annotations

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
    "quarantined_property_ids": [12014],
    "property_link_evidence": {
        "method": "read_only_repeatable_read",
        "observed_at": "2026-10-19T06:56:52.398653+00:00",
        "snapshot_sha256": "b35cefab2b62664c0adbb4da494522ef616e293a7aeaed1b736dde83cbdfab8e",
        "property_identity_status": "provisional",
        "property_link_count": 1,
        "scope": "conservative_database_key_hold_not_confirmed_physical_identity",
    },
},)
QUARANTINED_SOURCE_IDS = frozenset(record["id"] for record in KNOWN_SOURCE_CONFLICTS)
QUARANTINED_PROPERTY_IDS = frozenset(value for record in KNOWN_SOURCE_CONFLICTS
                                     for value in record.get("quarantined_property_ids", ()))


def quarantine_sale_rows(rows: list[dict], *, linked_listing_ids=()) -> list[dict]:
    """Exclude known negatives and relists without inheriting source receipts.

    The publisher supplies the complete bounded database-link expansion even
    when the original source is inactive. Deriving links from present rows is
    additional pure protection, not a claim of complete physical identity.
    A multi-property join cannot retain another copy of a quarantined listing.
    """
    blocked_ids = QUARANTINED_SOURCE_IDS | {str(value) for value in linked_listing_ids}
    held_properties = QUARANTINED_PROPERTY_IDS | {row.get("property_id") for row in rows
                       if str(row.get("id")) in blocked_ids and row.get("property_id") is not None}
    blocked_ids |= {str(row.get("id")) for row in rows if row.get("property_id") in held_properties}
    return [row for row in rows if str(row.get("id")) not in blocked_ids]
