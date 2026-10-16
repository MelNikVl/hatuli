"""Eligible canonical complex pages for search discovery.

The activity count is only an inclusion/ranking signal, not a market metric.
Keep quarantined identities and redirect aliases out of the public sitemap.
"""

COMPLEX_SITEMAP_SQL = """
    SELECT c.id, c.updated_at,
           COUNT(a.id) FILTER (
               WHERE a.is_active IS NOT FALSE AND COALESCE(a.is_duplicate, FALSE) = FALSE
           ) AS active_cnt
    FROM complexes c
    LEFT JOIN apartment_listings a ON lower(trim(a.complex_name)) = lower(trim(c.name))
    WHERE COALESCE(c.is_street, FALSE) = FALSE
      AND COALESCE(c.is_garbage, FALSE) = FALSE
      AND c.canonical_id IS NULL
      AND COALESCE(c.canonical_reason, '') <> 'junk_unmatched'
    GROUP BY c.id
    HAVING COUNT(a.id) FILTER (
        WHERE a.is_active IS NOT FALSE AND COALESCE(a.is_duplicate, FALSE) = FALSE
    ) > 0
    ORDER BY active_cnt DESC, c.id
    LIMIT 2000
"""
