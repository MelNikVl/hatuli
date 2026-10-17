"""Eligible canonical complex pages for search discovery.

The activity count is only an inclusion/ranking signal, not a market metric.
Keep quarantined identities and redirect aliases out of the public sitemap.
"""

from bot.core.complex_membership import active_sale_membership_ctes_sql


COMPLEX_SITEMAP_SQL = active_sale_membership_ctes_sql(include_children=True) + """
    SELECT c.id, c.updated_at, COUNT(*) AS active_cnt
    FROM complexes c
    JOIN listing_membership member ON member.complex_id = c.id
    GROUP BY c.id
    ORDER BY active_cnt DESC, c.id
    LIMIT 2000
"""
