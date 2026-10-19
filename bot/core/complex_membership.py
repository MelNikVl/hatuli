"""One membership rule for canonical complex pages and their location evidence.

Explicit listing IDs are authoritative. Legacy names are a fallback only for
unbound listings and only when all eligible rows with that name share one
canonical ID. A resolved child belongs to the child, not also to its umbrella.
"""
from __future__ import annotations

import re


def _eligible_complex_sql(alias: str) -> str:
    return (f"COALESCE({alias}.is_garbage, FALSE) = FALSE "
            f"AND COALESCE({alias}.is_street, FALSE) = FALSE "
            f"AND COALESCE({alias}.canonical_reason, '') <> 'junk_unmatched'")


def listing_complex_match_sql(complex_param: str = "$1", listing_alias: str | None = None,
                              *, include_children: bool = False) -> str:
    """Return a predicate for apartment_listings and a canonical complex ID.

    The parameter and optional table alias are code identifiers, never user
    input. IN/EXISTS keeps duplicate names from multiplying listing rows.
    """
    if not re.fullmatch(r"\$[1-9][0-9]*", complex_param):
        raise ValueError("complex_param must be a PostgreSQL parameter")
    if listing_alias is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", listing_alias):
        raise ValueError("listing_alias must be a SQL identifier")
    prefix = (listing_alias + ".") if listing_alias else "apartment_listings."

    eligible = _eligible_complex_sql

    def name_matches(alias: str) -> str:
        return (f"(complex_name_key({alias}.name) = complex_name_key({prefix}complex_name) "
                f"OR EXISTS (SELECT 1 FROM complex_aliases ca WHERE ca.complex_id = {alias}.id "
                f"AND ca.status = 'verified' AND ca.normalized_name = complex_name_key({prefix}complex_name)))")

    family = (f"SELECT family.id FROM complexes family WHERE {eligible('family')} "
              f"AND (family.id = {complex_param}")
    if include_children:
        family += (" OR family.parent_complex_id IN (SELECT parent.id FROM complexes parent "
                   f"WHERE {eligible('parent')} AND COALESCE(parent.canonical_id, parent.id) = {complex_param})")
    family += ")"
    ids = (f"SELECT cm.id FROM complexes cm WHERE {eligible('cm')} "
           f"AND COALESCE(cm.canonical_id, cm.id) IN ({family})")
    return f"""(
        {prefix}complex_id IN ({ids})
        OR ({prefix}complex_id IS NULL AND {prefix}complex_resolution IS DISTINCT FROM 'unbound' AND (
            {prefix}resolved_house_id IN ({ids})
            OR ({prefix}resolved_house_id IS NULL AND EXISTS (
                SELECT 1 FROM complexes cm
                 WHERE {eligible('cm')}
                   AND COALESCE(cm.canonical_id, cm.id) IN ({family})
                   AND NULLIF(btrim({prefix}complex_name), '') IS NOT NULL
                   AND {name_matches('cm')}
                   AND NOT EXISTS (
                       SELECT 1 FROM complexes rival
                        WHERE {eligible('rival')}
                          AND {name_matches('rival')}
                          AND COALESCE(rival.canonical_id, rival.id) <> COALESCE(cm.canonical_id, cm.id)
                   )
            ))
        ))
    )"""


def listing_complex_column_match_sql(complex_column: str, listing_alias: str | None = None,
                                     *, include_children: bool = False) -> str:
    """Use the same membership inside a correlated query with a trusted column."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*", complex_column):
        raise ValueError("complex_column must be a qualified SQL identifier")
    return listing_complex_match_sql("$1", listing_alias, include_children=include_children).replace("$1", complex_column)


def active_sale_membership_ctes_sql(*, include_children: bool = False) -> str:
    """Current active nonduplicate membership for sitemap/aggregate consumers."""
    return sale_membership_ctes_sql(include_children=include_children, active_only=True,
                                   nonduplicate_only=True)


def sale_membership_ctes_sql(*, include_children: bool = False, active_only: bool = False,
                            nonduplicate_only: bool = False) -> str:
    """Bulk equivalent of listing_complex_match_sql for eligible canonical pages.

    Resolve listing identities once instead of running a correlated
    identity predicate for every listing/page pair. Keep the existing one-hop
    canonical semantics, explicit FK priority and unique verified-name fallback.
    Returns a WITH clause exposing listing_membership(listing_id, complex_id).
    """
    filters = []
    if active_only:
        filters.append("a.is_active IS NOT FALSE")
    if nonduplicate_only:
        filters.append("COALESCE(a.is_duplicate, FALSE) = FALSE")
    where = "WHERE " + " AND ".join(filters) if filters else ""
    parents = """
        UNION
        SELECT owner.listing_id, page.id AS complex_id
        FROM membership_owners owner
        JOIN membership_complexes child ON child.id = owner.cid
        JOIN membership_complexes parent ON parent.id = child.parent_complex_id
        JOIN membership_complexes page ON page.id = parent.cid AND page.canonical_id IS NULL
    """ if include_children else ""
    return f"""WITH membership_complexes AS MATERIALIZED (
        SELECT c.id, c.name, c.canonical_id, COALESCE(c.canonical_id, c.id) AS cid,
               c.parent_complex_id
        FROM complexes c WHERE {_eligible_complex_sql('c')}
    ), membership_names AS (
        SELECT complex_name_key(c.name) AS n, c.cid FROM membership_complexes c
        UNION
        SELECT ca.normalized_name, c.cid
        FROM complex_aliases ca JOIN membership_complexes c ON c.id = ca.complex_id
        WHERE ca.status = 'verified'
    ), membership_unique_names AS MATERIALIZED (
        SELECT n, min(cid) AS cid FROM membership_names
        WHERE n <> '' GROUP BY n HAVING count(DISTINCT cid) = 1
    ), membership_owners AS MATERIALIZED (
        SELECT a.id AS listing_id,
               CASE WHEN a.complex_id IS NOT NULL THEN explicit.cid
                    WHEN a.complex_resolution = 'unbound' THEN NULL
                    WHEN a.resolved_house_id IS NOT NULL THEN house.cid
                    ELSE name.cid END AS cid
        FROM apartment_listings a
        LEFT JOIN membership_complexes explicit ON explicit.id = a.complex_id
        LEFT JOIN membership_complexes house ON house.id = a.resolved_house_id
        LEFT JOIN membership_unique_names name
          ON name.n = complex_name_key(a.complex_name)
         AND NULLIF(btrim(a.complex_name), '') IS NOT NULL
        {where}
    ), listing_membership AS (
        SELECT owner.listing_id, page.id AS complex_id
        FROM membership_owners owner
        JOIN membership_complexes page ON page.id = owner.cid AND page.canonical_id IS NULL
        {parents}
    )"""


def listing_complex_page_match_sql(complex_param: str = "$1", *, include_children: bool = False) -> str:
    """Uncorrelated ID set for a canonical page; keep archived/duplicate rows.

    The caller retains its own activity/duplicate filters, just as with the
    single-row predicate. This is current membership, not an as_of feature.
    """
    if not re.fullmatch(r"\$[1-9][0-9]*", complex_param):
        raise ValueError("complex_param must be a PostgreSQL parameter")
    return f"""apartment_listings.id IN (
        {sale_membership_ctes_sql(include_children=include_children)}
        SELECT listing_id FROM listing_membership WHERE complex_id = {complex_param}
    )"""


def rental_identity_ctes_sql(*, materialize_roots: bool = False) -> str:
    """One read-only identity map for rental membership and asking estimates.

    Redirects terminate at an eligible root. Cycles, missing roots, ambiguous
    source URLs and names never select an arbitrary physical project. The
    asking-rent publisher materializes roots so PostgreSQL does not repeat
    their correlated child/umbrella check inside the rental-row join.
    """
    roots_materialization = "MATERIALIZED " if materialize_roots else ""
    return f"""WITH RECURSIVE rental_redirects AS (
        SELECT c.id AS original_id,c.id,c.canonical_id,ARRAY[c.id] AS visited
        FROM complexes c
        UNION ALL
        SELECT walk.original_id,c.id,c.canonical_id,walk.visited || c.id
        FROM rental_redirects walk JOIN complexes c ON c.id=walk.canonical_id
        WHERE NOT c.id=ANY(walk.visited) AND cardinality(walk.visited)<32
    ), rental_canonical_map AS (
        SELECT walk.original_id AS id,root.id AS cid
        FROM rental_redirects walk JOIN complexes root ON root.id=walk.id
        WHERE walk.canonical_id IS NULL AND NOT coalesce(root.is_garbage,false)
          AND NOT coalesce(root.is_street,false)
          AND root.canonical_reason IS DISTINCT FROM 'junk_unmatched'
    ), cx AS (
        SELECT map.id,map.cid,complex_name_key(c.name) AS n
        FROM rental_canonical_map map JOIN complexes c ON c.id=map.id
    ), rental_roots AS {roots_materialization}(
        SELECT c.id,c.name,parent.cid AS parent_id,
               (coalesce(c.is_umbrella,false) OR EXISTS (
                   SELECT 1 FROM complexes child JOIN rental_canonical_map p ON p.id=child.parent_complex_id
                   WHERE p.cid=c.id AND child.canonical_id IS NULL
                     AND NOT coalesce(child.is_garbage,false) AND NOT coalesce(child.is_street,false)
                     AND child.canonical_reason IS DISTINCT FROM 'junk_unmatched'
               )) AS is_umbrella
        FROM complexes c JOIN rental_canonical_map own ON own.id=c.id AND own.cid=c.id
        LEFT JOIN rental_canonical_map parent ON parent.id=c.parent_complex_id
    ), names AS (
        SELECT n,cid FROM cx
        UNION ALL
        SELECT a.normalized_name,cx.cid FROM complex_aliases a JOIN cx ON cx.id=a.complex_id
        WHERE a.status='verified'
    ), name_keys AS (
        SELECT n,min(cid) AS cid FROM names WHERE n<>''
        GROUP BY n HAVING count(DISTINCT cid)=1
    ), rental_source_links AS (
        SELECT rtrim(split_part(split_part(c.krisha_url,'?',1),'#',1),'/') AS url,cx.cid
        FROM complexes c JOIN cx ON cx.id=c.id WHERE c.krisha_url IS NOT NULL
        UNION ALL
        SELECT rtrim(split_part(split_part(sl.url,'?',1),'#',1),'/'),cx.cid
        FROM complex_source_links sl JOIN cx ON cx.id=sl.complex_id
        WHERE sl.source='krisha' AND (sl.match_method='manual' OR sl.confidence>=0.85)
    ), rental_source_keys AS (
        SELECT url,min(cid) AS cid FROM rental_source_links GROUP BY url
        HAVING count(DISTINCT cid)=1
    )"""


def rental_complex_match_sql(complex_param: str = "$1", listing_alias: str | None = None,
                             *, include_children: bool = False) -> str:
    """Source-gated FK first; a unique name is only for a NULL rental FK.

    A source umbrella may support an explicitly resolved child, never infer
    that child from its name or proximity. Existing FKs cannot be overridden.
    """
    if not re.fullmatch(r"\$[1-9][0-9]*", complex_param):
        raise ValueError("complex_param must be a PostgreSQL parameter")
    if listing_alias is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", listing_alias):
        raise ValueError("listing_alias must be a SQL identifier")
    prefix = (listing_alias + ".") if listing_alias else "rental_listings."

    target = f"(SELECT cid FROM rental_canonical_map WHERE id={complex_param})"
    family = f"canonical.id={target}"
    if include_children:
        family += f" OR canonical.parent_id={target}"
    return f"""EXISTS (
        {rental_identity_ctes_sql()}
        SELECT 1 FROM rental_roots canonical
        JOIN rental_source_keys source ON
          {prefix}complex_url ~ '^https://krisha[.]kz/complex/show/[^/?#]+(/[^/?#]+)?/?$'
          AND source.url=rtrim({prefix}complex_url,'/')
          AND (source.cid=canonical.id OR
               ({prefix}complex_id IS NOT NULL AND source.cid=canonical.parent_id))
        WHERE (
            ({prefix}complex_id IS NOT NULL AND canonical.id=(
                SELECT cid FROM rental_canonical_map WHERE id={prefix}complex_id))
            OR ({prefix}complex_id IS NULL AND canonical.id=(
                SELECT cid FROM name_keys WHERE n=complex_name_key({prefix}complex_name)))
        ) AND ({family})
    )"""


def rental_complex_page_match_sql(complex_param: str = "$1", *, include_children: bool = False) -> str:
    """Uncorrelated source-gated rental ID set, equivalent to the page predicate."""
    if not re.fullmatch(r"\$[1-9][0-9]*", complex_param):
        raise ValueError("complex_param must be a PostgreSQL parameter")
    target = f"(SELECT cid FROM rental_canonical_map WHERE id={complex_param})"
    family = f"canonical.id={target}"
    if include_children:
        family += f" OR canonical.parent_id={target}"
    return f"""rental_listings.id IN (
        {rental_identity_ctes_sql(materialize_roots=True)}
        SELECT r.id FROM rental_listings r
        LEFT JOIN rental_canonical_map bound ON bound.id=r.complex_id
        LEFT JOIN name_keys nk ON r.complex_id IS NULL AND nk.n=complex_name_key(r.complex_name)
        JOIN rental_roots canonical ON canonical.id=coalesce(bound.cid,nk.cid)
        JOIN rental_source_keys source ON
          r.complex_url ~ '^https://krisha[.]kz/complex/show/[^/?#]+(/[^/?#]+)?/?$'
          AND source.url=rtrim(r.complex_url,'/')
          AND (source.cid=canonical.id OR (r.complex_id IS NOT NULL AND source.cid=canonical.parent_id))
        WHERE {family}
    )"""


def unit_complex_match_sql(complex_param: str = "$1", unit_alias: str | None = None,
                           *, include_children: bool = False) -> str:
    """Read preserved newbuild unit FKs through canonical aliases/families."""
    if not re.fullmatch(r"\$[1-9][0-9]*", complex_param):
        raise ValueError("complex_param must be a PostgreSQL parameter")
    if unit_alias is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", unit_alias):
        raise ValueError("unit_alias must be a SQL identifier")
    prefix = unit_alias + "." if unit_alias else "newbuild_units."
    children = ""
    if include_children:
        children = (" OR family.parent_complex_id IN (SELECT parent.id FROM complexes parent "
                    f"WHERE COALESCE(parent.canonical_id, parent.id) = {complex_param})")
    return f"""{prefix}complex_id IN (
        SELECT owner.id FROM complexes owner
        WHERE COALESCE(owner.canonical_id, owner.id) IN (
            SELECT family.id FROM complexes family
            WHERE COALESCE(family.is_garbage, FALSE) = FALSE
              AND COALESCE(family.is_street, FALSE) = FALSE
              AND COALESCE(family.canonical_reason, '') <> 'junk_unmatched'
              AND (family.id = {complex_param}{children})
        )
    )"""
