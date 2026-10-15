"""Conservative identity lookup for imports, with durable source observations.

An existing external ID takes precedence over names. Otherwise only an exact
normalized catalog name or a reviewed alias may identify a complex. Fuzzy
spellings and addresses never establish identity here. Ambiguity is an error,
so an import cannot silently select one project or create another duplicate.
"""
from __future__ import annotations

import json
import re

from bot.core.complex_canonical import norm_name

normalize_complex_name = norm_name
RESOLVER_VERSION = "complex_ingest_identity_v1"


class AmbiguousComplexIdentity(ValueError):
    def __init__(self, name: str, candidate_ids: set[int]):
        self.candidate_ids = tuple(sorted(candidate_ids))
        super().__init__(f"Ambiguous complex name {name!r}: canonical IDs {self.candidate_ids}")


class InvalidComplexIdentity(ValueError):
    """An existing identity link cannot safely be followed."""


def _database(connection):
    if connection is not None:
        return connection
    from bot.db import pg
    return pg


async def canonical_complex_id(complex_id: int, *, connection=None) -> int:
    """Follow redirects without changing the source spine; fail on bad roots."""
    db = _database(connection)
    seen: set[int] = set()
    current = complex_id
    while current not in seen:
        seen.add(current)
        row = await db.fetchrow(
            "SELECT id, canonical_id, canonical_reason, is_garbage, is_street "
            "FROM complexes WHERE id = $1", current)
        if row is None:
            raise InvalidComplexIdentity(f"Missing complex {current} in redirect from {complex_id}")
        if row["canonical_id"] is not None:
            current = row["canonical_id"]
            continue
        if row["is_garbage"] or row["is_street"] or row["canonical_reason"] == "junk_unmatched":
            raise InvalidComplexIdentity(f"Ineligible canonical complex {current} for {complex_id}")
        return current
    raise InvalidComplexIdentity(f"Canonical cycle from complex {complex_id}: {sorted(seen)}")


async def resolve_ingest_complex(source: str, source_id: str, name: str, *, connection=None) -> int | None:
    """Return one eligible canonical ID or None; never resolve ambiguity."""
    db = _database(connection)
    linked = await db.fetchrow(
        "SELECT complex_id FROM complex_source_links WHERE source = $1 AND source_id = $2",
        source, str(source_id))
    if linked is None:
        # Legacy one-slot IDs are still external identity evidence. Without
        # this fallback an old parser row lacking a spine backfill could be
        # duplicated as soon as its project name changes.
        linked = await db.fetchrow(
            "SELECT id AS complex_id FROM complexes "
            "WHERE newbuild_source = $1 AND newbuild_source_id = $2",
            source, str(source_id))
    if linked is not None:
        return await canonical_complex_id(linked["complex_id"], connection=db)
    # Aggregate catalogs historically kept stable URLs only in source_info or
    # a legacy URL column. Reuse that source identity before a renamed label.
    if str(source_id).startswith(("https://", "http://")):
        legacy_urls = await db.fetch("""
            SELECT id FROM complexes
            WHERE source_info -> $1::text ->> 'url' = $2
               OR ($1 = 'korter' AND korter_url = $2)
               OR ($1 = 'krisha' AND krisha_url = $2)
        """, source, str(source_id))
        legacy_ids = {await canonical_complex_id(row["id"], connection=db) for row in legacy_urls}
        if len(legacy_ids) > 1:
            raise AmbiguousComplexIdentity(name, legacy_ids)
        if legacy_ids:
            return next(iter(legacy_ids))
    return await resolve_complex_name(name, connection=db, strict=True)


async def resolve_complex_name(name: str | None, *, connection=None, strict: bool = False) -> int | None:
    """Safe name-only fallback; strict imports report ambiguity instead of seeding.

    Other consumers receive None for empty, ambiguous or invalid names. Exact
    normalized catalog names and verified aliases share the same candidate set.
    """
    db = _database(connection)
    key = normalize_complex_name(name)
    if not key:
        if strict:
            raise InvalidComplexIdentity("An empty normalized complex name cannot create an identity")
        return None
    candidates = await db.fetch("""
        SELECT c.id FROM complexes c
         WHERE complex_name_key(c.name) = complex_name_key($1)
           AND COALESCE(c.is_garbage, FALSE) = FALSE
           AND COALESCE(c.is_street, FALSE) = FALSE
           AND COALESCE(c.canonical_reason, '') <> 'junk_unmatched'
        UNION
        SELECT c.id FROM complex_aliases a JOIN complexes c ON c.id = a.complex_id
         WHERE a.normalized_name = complex_name_key($1) AND a.status = 'verified'
           AND COALESCE(c.is_garbage, FALSE) = FALSE
           AND COALESCE(c.is_street, FALSE) = FALSE
           AND COALESCE(c.canonical_reason, '') <> 'junk_unmatched'
    """, name)
    try:
        ids = {await canonical_complex_id(row["id"], connection=db) for row in candidates}
    except InvalidComplexIdentity:
        if strict:
            raise
        return None
    if len(ids) > 1:
        if strict:
            raise AmbiguousComplexIdentity(name, ids)
        return None
    return next(iter(ids)) if ids else None


async def record_complex_observations(
    complex_id: int, source: str, source_id: str, name: str | None,
    address: str | None, *, evidence: dict | None = None, connection=None,
) -> None:
    """Idempotently retain raw values, preserving reviewer decisions on reimport."""
    db = _database(connection)
    evidence_json = json.dumps(evidence or {}, ensure_ascii=False, default=str)
    key = normalize_complex_name(name)
    if name and name.strip() and key:
        await db.execute("""
            INSERT INTO complex_aliases
                (complex_id, name, normalized_name, source, source_id, evidence)
            VALUES ($1, $2, complex_name_key($2), $3, $4, $5::jsonb)
            ON CONFLICT (complex_id, source, source_id, name) DO UPDATE SET
                last_seen_at = now(), evidence = complex_aliases.evidence || EXCLUDED.evidence
        """, complex_id, name, source, str(source_id), evidence_json)
    if address and address.strip():
        address_key = re.sub(r"\s+", " ", address).strip().lower()
        await db.execute("""
            INSERT INTO complex_addresses
                (complex_id, address, normalized_address, source, source_id, evidence)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (complex_id, source, source_id, address) DO UPDATE SET
                last_seen_at = now(), evidence = complex_addresses.evidence || EXCLUDED.evidence
        """, complex_id, address, address_key, source, str(source_id), evidence_json)
