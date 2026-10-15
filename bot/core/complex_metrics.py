"""Current complex metrics and coordinate evidence use stable identities."""

IDENTITY_CTES = """
WITH cx AS (
    SELECT id, COALESCE(canonical_id, id) AS cid, complex_name_key(name) AS n
    FROM complexes WHERE COALESCE(is_garbage, FALSE) = FALSE
      AND COALESCE(is_street, FALSE) = FALSE
      AND COALESCE(canonical_reason, '') <> 'junk_unmatched'
), names AS (
    SELECT n, cid FROM cx
    UNION ALL
    SELECT ca.normalized_name, cx.cid FROM complex_aliases ca JOIN cx ON cx.id = ca.complex_id
    WHERE ca.status = 'verified'
), name_keys AS (
    SELECT n, min(cid) AS cid FROM names WHERE n <> ''
    GROUP BY n HAVING count(DISTINCT cid) = 1
), listings AS (
    SELECT a.*, COALESCE(bound.cid, house.cid, nk.cid) AS cid
    FROM apartment_listings a
    LEFT JOIN cx bound ON bound.id = a.complex_id
    LEFT JOIN cx house ON a.complex_id IS NULL AND a.complex_resolution IS DISTINCT FROM 'unbound'
                     AND house.id = a.resolved_house_id
    LEFT JOIN name_keys nk ON a.complex_id IS NULL AND a.complex_resolution IS DISTINCT FROM 'unbound'
                         AND a.resolved_house_id IS NULL
                         AND nk.n = complex_name_key(a.complex_name)
    WHERE COALESCE(a.is_duplicate, FALSE) = FALSE
)
"""

CURRENT_METRICS_SQL = IDENTITY_CTES + """,
membership AS (
    SELECT l.id, l.cid FROM listings l WHERE l.cid IS NOT NULL
    UNION
    SELECT l.id, parent.id
    FROM listings l JOIN complexes child ON child.id = l.cid
    JOIN complexes parent_alias ON parent_alias.id = child.parent_complex_id
    JOIN complexes parent ON parent.id = COALESCE(parent_alias.canonical_id,parent_alias.id)
    WHERE parent.is_umbrella AND COALESCE(parent.is_garbage, FALSE) = FALSE
      AND COALESCE(parent.is_street, FALSE) = FALSE
), stats AS (
    SELECT m.cid,
           count(*) FILTER (WHERE l.is_active IS NOT FALSE) AS active_count,
           count(*) FILTER (WHERE l.is_active IS FALSE) AS sold_count,
           avg(l.price / NULLIF(l.area, 0)) FILTER (WHERE l.is_active IS NOT FALSE) AS price_m2,
           avg(l.yield_pct) FILTER (WHERE l.is_active IS NOT FALSE AND l.yield_pct > 0) AS yield_pct
    FROM membership m JOIN listings l ON l.id = m.id GROUP BY m.cid
), rental_membership AS (
    SELECT r.id, nk.cid FROM rental_listings r JOIN name_keys nk ON nk.n = complex_name_key(r.complex_name)
    WHERE r.last_seen > now() - interval '14 days'
    UNION
    SELECT r.id, parent.id
    FROM rental_listings r JOIN name_keys nk ON nk.n = complex_name_key(r.complex_name)
    JOIN complexes child ON child.id = nk.cid
    JOIN complexes parent_alias ON parent_alias.id = child.parent_complex_id
    JOIN complexes parent ON parent.id = COALESCE(parent_alias.canonical_id,parent_alias.id)
    WHERE r.last_seen > now() - interval '14 days' AND parent.is_umbrella
), rentals AS (SELECT cid, count(*) AS n FROM rental_membership GROUP BY cid)
UPDATE complexes c SET
    listings_count = COALESCE(s.active_count, 0), sold_count = COALESCE(s.sold_count, 0),
    avg_price_m2 = s.price_m2, avg_yield = s.yield_pct, rental_listings_count = COALESCE(r.n, 0)
FROM complexes target LEFT JOIN stats s ON s.cid = target.id
LEFT JOIN rentals r ON r.cid = target.id
WHERE c.id = target.id
"""

# Coordinates remain observations. Trim distant listings around the median,
# require three supporting points, and fill only missing canonical locations.
CENTROID_SQL = IDENTITY_CTES + """,
medians AS (
    SELECT cid, percentile_cont(0.5) WITHIN GROUP (ORDER BY lat) AS lat,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY lon) AS lon
    FROM listings WHERE lat BETWEEN 50.8 AND 51.5 AND lon BETWEEN 71.0 AND 71.9
    GROUP BY cid
), centers AS (
    SELECT l.cid, avg(l.lat) AS lat, avg(l.lon) AS lon
    FROM listings l JOIN medians m ON m.cid = l.cid
    WHERE sqrt(((l.lat - m.lat) * 111000)^2 +
               ((l.lon - m.lon) * 111000 * cos(radians(m.lat)))^2) <= 150
    GROUP BY l.cid HAVING count(*) >= 3
)
UPDATE complexes c SET lat = s.lat, lon = s.lon, coords_source = 'listings'
FROM centers s WHERE c.id = s.cid AND c.canonical_id IS NULL
  AND (c.lat IS NULL OR c.lon IS NULL)
"""
