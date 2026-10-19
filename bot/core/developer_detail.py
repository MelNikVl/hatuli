"""Current developer projects with one shared sale-membership calculation.

The public page historically includes archived listings and uses duplicates
for its modal address, while excluding them from archive-duration averages.
Those scopes must stay separate. Nothing here interprets archiving as a sale
or changes listing identities, cached counts, or access rules.
"""
from bot.core.complex_membership import sale_membership_ctes_sql


DEVELOPER_COMPLEXES_SQL = sale_membership_ctes_sql(include_children=True) + """,
developer_complexes AS MATERIALIZED (
    SELECT c.id, c.name, c.district, c.year_built, c.housing_class,
           c.photo_url, c.lat, c.lon,
           COALESCE(c.listings_count, 0) AS active_cnt,
           COALESCE(c.sold_count, 0) AS sold_cnt, c.avg_price_m2
    FROM complexes c
    WHERE c.developer_id = $1
      AND COALESCE(c.is_street, FALSE) = FALSE
      AND COALESCE(c.is_garbage, FALSE) = FALSE
      AND c.canonical_id IS NULL
      AND COALESCE(c.canonical_reason, '') <> 'junk_unmatched'
), developer_listing_data AS MATERIALIZED (
    SELECT member.complex_id, al.address, al.is_duplicate, al.archived_at, al.first_seen
    FROM listing_membership member
    JOIN developer_complexes c ON c.id = member.complex_id
    JOIN apartment_listings al ON al.id = member.listing_id
), developer_durations AS (
    SELECT complex_id,
           AVG(EXTRACT(EPOCH FROM (archived_at - first_seen))/86400)
             FILTER (WHERE archived_at IS NOT NULL
                           AND COALESCE(is_duplicate, FALSE) = FALSE) AS avg_days_to_sell
    FROM developer_listing_data GROUP BY complex_id
), developer_address_counts AS (
    SELECT complex_id, address, COUNT(*) AS cnt
    FROM developer_listing_data
    WHERE address IS NOT NULL AND address != ''
    GROUP BY complex_id, address
), developer_addresses AS (
    SELECT complex_id, address,
           ROW_NUMBER() OVER (PARTITION BY complex_id ORDER BY cnt DESC, address) AS position
    FROM developer_address_counts
)
SELECT c.id, c.name, c.district, c.year_built, c.housing_class,
       c.photo_url, c.lat, c.lon, c.active_cnt, c.sold_cnt, c.avg_price_m2,
       g.avg_days_to_sell, a.address
FROM developer_complexes c
LEFT JOIN developer_durations g ON g.complex_id = c.id
LEFT JOIN developer_addresses a ON a.complex_id = c.id AND a.position = 1
ORDER BY c.active_cnt DESC, c.sold_cnt DESC
"""
