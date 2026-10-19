"""Eligible canonical complex pages for search discovery.

The activity count is only an inclusion/ranking signal, not a market metric.
Keep quarantined identities and redirect aliases out of the public sitemap.
"""

from datetime import date, datetime
import re

from bot.core.complex_membership import active_sale_membership_ctes_sql


PUBLIC_ORIGIN = "https://hatuli.ai-groundtruth.com"
_ARTICLE_URL = re.compile(r"/analytics/(?P<day>\d{4}-\d{2}-\d{2})-(?:market|rent)(?:-local)?\Z")
# Keep the existing explicit AI crawler permissions. All listed user agents
# share the same private-path exclusions instead of accidentally giving those
# exclusions only to the last group (formerly Yandex).
CRAWLER_USER_AGENTS = ("*", "GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot",
                      "Claude-SearchBot", "Claude-User", "PerplexityBot",
                      "Google-Extended", "Applebot-Extended", "Yandex")
PRIVATE_CRAWL_PATHS = ("/admin", "/api/", "/auth/", "/cabinet", "/favorites",
                       "/request-analysis", "/buyer/map/api/", "/rental/daily", "/rental/scanner")
# Existing anonymous GET resources used to render the public newbuild map and
# the catalogue map. Endpoint authorization and source gates are unchanged.
# Exact-end/query pairs avoid allowing similarly prefixed operator endpoints.
PUBLIC_RENDER_PATHS = ("/admin/api/newbuild-map-points", "/admin/api/newbuild-developers",
                       "/admin/api/complexes-map", "/admin/api/complexes-layer")


def robots_document() -> str:
    """Crawl instructions only; route authorization remains the access control."""
    lines = [f"User-agent: {agent}" for agent in CRAWLER_USER_AGENTS]
    for path in PUBLIC_RENDER_PATHS:
        lines.extend((f"Allow: {path}$", f"Allow: {path}?"))
    lines.extend((*(f"Disallow: {path}" for path in PRIVATE_CRAWL_PATHS),
                  "Allow: /", "", f"Sitemap: {PUBLIC_ORIGIN}/sitemap.xml", ""))
    return "\n".join(lines)


def article_sitemap_entry(article: dict) -> tuple[str, str] | None:
    """Only canonical article paths; lastmod is a real editorial timestamp.

    Validated revisions expose ``date_modified`` without retiming the original
    edition. Older normalized archives can fall back to publication time, and
    date-only legacy records retain their genuine edition date. Request time,
    archive reads and filesystem mtimes never create a modification date.
    """
    url = article.get("url")
    if not isinstance(url, str) or not (match := _ARTICLE_URL.fullmatch(url)):
        return None
    try:
        edition_day = date.fromisoformat(match["day"])
    except ValueError:
        return None
    for key in ("date_modified", "published_at"):
        value = article.get(key)
        if not isinstance(value, str):
            continue
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if timestamp.tzinfo is not None and timestamp.utcoffset() is not None:
                return PUBLIC_ORIGIN + url, timestamp.isoformat()
        except ValueError:
            continue
    return PUBLIC_ORIGIN + url, edition_day.isoformat()


COMPLEX_SITEMAP_SQL = active_sale_membership_ctes_sql(include_children=True) + """
    SELECT c.id, c.updated_at, COUNT(*) AS active_cnt
    FROM complexes c
    JOIN listing_membership member ON member.complex_id = c.id
    GROUP BY c.id
    ORDER BY active_cnt DESC, c.id
    LIMIT 2000
"""
