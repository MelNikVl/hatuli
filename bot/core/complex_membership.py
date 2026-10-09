"""One membership rule for canonical complex pages and their location evidence.

Explicit listing IDs are authoritative. Legacy names are a fallback only for
unbound listings and only when all eligible rows with that name share one
canonical ID. A resolved child belongs to the child, not also to its umbrella.
"""
from __future__ import annotations

import re


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

    def eligible(alias: str) -> str:
        return (f"COALESCE({alias}.is_garbage, FALSE) = FALSE "
                f"AND COALESCE({alias}.is_street, FALSE) = FALSE "
                f"AND COALESCE({alias}.canonical_reason, '') <> 'junk_unmatched'")

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


def rental_complex_match_sql(complex_param: str = "$1", listing_alias: str | None = None,
                             *, include_children: bool = False) -> str:
    """Rental names/verified aliases may match only one eligible identity family.

    Rentals have no stable complex FK yet. IN/EXISTS avoids multiplying rows,
    and a name shared with another physical project is left unassigned.
    """
    if not re.fullmatch(r"\$[1-9][0-9]*", complex_param):
        raise ValueError("complex_param must be a PostgreSQL parameter")
    if listing_alias is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", listing_alias):
        raise ValueError("listing_alias must be a SQL identifier")
    prefix = (listing_alias + ".") if listing_alias else "rental_listings."

    def eligible(alias: str) -> str:
        return (f"COALESCE({alias}.is_garbage, FALSE) = FALSE "
                f"AND COALESCE({alias}.is_street, FALSE) = FALSE "
                f"AND COALESCE({alias}.canonical_reason, '') <> 'junk_unmatched'")

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
    return f"""(
        NULLIF(btrim({prefix}complex_name), '') IS NOT NULL AND EXISTS (
            SELECT 1 FROM complexes cm WHERE {eligible('cm')}
              AND COALESCE(cm.canonical_id, cm.id) IN ({family})
              AND {name_matches('cm')}
              AND NOT EXISTS (
                  SELECT 1 FROM complexes rival WHERE {eligible('rival')}
                    AND {name_matches('rival')}
                    AND COALESCE(rival.canonical_id, rival.id) <> COALESCE(cm.canonical_id, cm.id)
              )
        )
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
