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
        OR ({prefix}complex_id IS NULL AND (
            {prefix}resolved_house_id IN ({ids})
            OR ({prefix}resolved_house_id IS NULL AND EXISTS (
                SELECT 1 FROM complexes cm
                 WHERE {eligible('cm')}
                   AND COALESCE(cm.canonical_id, cm.id) IN ({family})
                   AND NULLIF(btrim({prefix}complex_name), '') IS NOT NULL
                   AND lower(btrim(cm.name)) = lower(btrim({prefix}complex_name))
                   AND NOT EXISTS (
                       SELECT 1 FROM complexes rival
                        WHERE {eligible('rival')}
                          AND lower(btrim(rival.name)) = lower(btrim(cm.name))
                          AND COALESCE(rival.canonical_id, rival.id) NOT IN ({family})
                   )
            ))
        ))
    )"""
