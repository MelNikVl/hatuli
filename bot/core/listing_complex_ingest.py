"""Bounded, conservative catalog seeding from apartment listing observations.

Raw spellings do not establish a second identity. Existing bindings win;
otherwise only a unique catalog name or reviewed alias can be reused. New
names share the same normalized-name lock as developer imports. This job
records evidence and creates catalog rows; it never changes listing bindings.
"""
from __future__ import annotations

from collections import defaultdict

from bot.core.complex_canonical import _CLASS_WORDS, _JUNK_RE, _LEADING_JUNK_RE, is_junk_name
from bot.core.complex_ingest_identity import (
    AmbiguousComplexIdentity, InvalidComplexIdentity, RESOLVER_VERSION,
    canonical_complex_id, record_complex_observations, resolve_complex_name,
)

SOURCE = "listing_snapshot"

# Only pending observations enter the bounded batch. Existing catalog keys,
# including rejected/garbage records, cannot be recreated by a raw spelling.
# Source observation lookups use the existing compound unique indexes.
_PENDING_SQL = """
WITH cx AS (
    SELECT id, COALESCE(canonical_id, id) AS cid, complex_name_key(name) AS n
    FROM complexes WHERE COALESCE(is_garbage, FALSE) = FALSE
      AND COALESCE(is_street, FALSE) = FALSE
      AND COALESCE(canonical_reason, '') <> 'junk_unmatched'
), names AS (
    SELECT n, cid FROM cx
    UNION
    SELECT a.normalized_name, cx.cid FROM complex_aliases a JOIN cx ON cx.id = a.complex_id
    WHERE a.status = 'verified'
), unique_names AS (
    SELECT n, min(cid) AS cid FROM names GROUP BY n HAVING count(DISTINCT cid) = 1
)
SELECT a.id, a.complex_name, a.address, a.district, a.complex_id, a.resolved_house_id, a.complex_resolution,
       complex_name_key(a.complex_name) AS name_key
FROM apartment_listings a
LEFT JOIN cx bound ON bound.id = a.complex_id
LEFT JOIN cx house ON a.complex_id IS NULL AND house.id = a.resolved_house_id
LEFT JOIN unique_names named ON a.complex_id IS NULL AND a.resolved_house_id IS NULL
                            AND named.n = complex_name_key(a.complex_name)
WHERE a.is_active IS NOT FALSE
  AND (a.complex_id IS NOT NULL OR COALESCE(a.complex_resolution, '') <> 'unbound')
  AND ($5::text[] IS NULL OR a.id = ANY($5::text[]))
  AND complex_name_key(a.complex_name) <> ''
  AND (COALESCE(bound.cid, house.cid, named.cid) IS NOT NULL OR (
       a.complex_name !~* $2 AND a.complex_name !~* $3
       AND complex_name_key(a.complex_name) <> ALL($4::text[])
       AND NOT (char_length(btrim(a.complex_name)) >= 30
                AND cardinality(regexp_split_to_array(btrim(a.complex_name), '\\s+')) >= 4)))
  AND (
    COALESCE(bound.cid, house.cid, named.cid) IS NOT NULL
    OR (a.complex_id IS NULL AND a.resolved_house_id IS NULL
        AND NOT EXISTS (SELECT 1 FROM complexes c
                         WHERE complex_name_key(c.name) = complex_name_key(a.complex_name))
        AND NOT EXISTS (SELECT 1 FROM complex_aliases ca
                         WHERE ca.normalized_name = complex_name_key(a.complex_name)))
  )
  AND (
    NOT EXISTS (SELECT 1 FROM complex_aliases ca
                 WHERE ca.complex_id = COALESCE(bound.cid, house.cid, named.cid)
                   AND ca.source = 'listing_snapshot' AND ca.source_id = a.id AND ca.name = a.complex_name)
    OR (NULLIF(btrim(a.address), '') IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM complex_addresses ca
         WHERE ca.complex_id = COALESCE(bound.cid, house.cid, named.cid)
           AND ca.source = 'listing_snapshot' AND ca.source_id = a.id AND ca.address = a.address))
  )
ORDER BY a.id LIMIT $1
"""


async def seed_listing_complexes(*, observation_limit: int = 500,
                                listing_ids: list[str] | None = None) -> dict:
    """Process at most observation_limit raw listings; no migrations or binding writes.

    A listing ID scope is useful to process a parser batch and in isolated
    tests. Without a scope the bounded pending queue gradually records the
    active catalog backlog. Observed aliases/addresses remain unreviewed.
    """
    if observation_limit < 1:
        raise ValueError("observation_limit must be positive")
    from bot.db.pg import get_pool

    result = {"examined": 0, "created": 0, "observed": 0, "skipped": 0, "busy": False}
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            locked = await conn.fetchval(
                "SELECT pg_try_advisory_xact_lock(hashtextextended($1, 0))",
                "complex_ingest:listing_snapshot")
            if not locked:
                return {**result, "busy": True}
            rows = await conn.fetch(_PENDING_SQL, observation_limit, _JUNK_RE.pattern,
                                    _LEADING_JUNK_RE.pattern, sorted(_CLASS_WORDS), listing_ids)
            result["examined"] = len(rows)
            groups = defaultdict(list)
            for row in rows:
                groups[row["name_key"]].append(row)

            for name_key in sorted(groups):
                await conn.fetchval("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                                    "complex_ingest:name:" + name_key)
                named_id = None
                name_resolved = False
                for row in groups[name_key]:
                    try:
                        explicit_id = row["complex_id"] if row["complex_id"] is not None else row["resolved_house_id"]
                        if explicit_id is not None:
                            cid = await canonical_complex_id(explicit_id, connection=conn)
                        else:
                            if not name_resolved:
                                named_id = await resolve_complex_name(row["complex_name"], strict=True, connection=conn)
                                name_resolved = True
                                if named_id is None:
                                    if is_junk_name(row["complex_name"]):
                                        result["skipped"] += 1
                                        continue
                                    # Recheck under the shared name lock. Ineligible
                                    # catalog/alias keys are blocks, not new projects.
                                    blocked = await conn.fetchval("""
                                        SELECT EXISTS(SELECT 1 FROM complexes WHERE complex_name_key(name) = $1)
                                            OR EXISTS(SELECT 1 FROM complex_aliases
                                                       WHERE normalized_name = $1)
                                    """, name_key)
                                    if not blocked:
                                        named_id = await conn.fetchval("""
                                            INSERT INTO complexes (name, district) VALUES ($1, $2)
                                            ON CONFLICT (lower(name)) DO NOTHING RETURNING id
                                        """, row["complex_name"].strip(), row["district"])
                                        if named_id is not None:
                                            result["created"] += 1
                                        else:
                                            named_id = await resolve_complex_name(row["complex_name"], strict=True, connection=conn)
                            cid = named_id
                        if cid is None:
                            result["skipped"] += 1
                            continue
                    except (AmbiguousComplexIdentity, InvalidComplexIdentity):
                        result["skipped"] += 1
                        continue
                    await record_complex_observations(
                        cid, SOURCE, row["id"], row["complex_name"], row["address"],
                        evidence={"listing_id": row["id"], "resolver_version": RESOLVER_VERSION,
                                  "complex_id_at_observation": row["complex_id"],
                                  "resolved_house_id_at_observation": row["resolved_house_id"]},
                        connection=conn,
                    )
                    result["observed"] += 1
    return result
