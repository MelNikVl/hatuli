"""Small allowlist for anonymous public-page analytics; never resolves sessions.

Authenticated visits are deliberately excluded, including public articles.
No cookie value, search text, source URL, calculator input, or request body is
returned to the template. The external counter receives only approved public
paths and bounded campaign labels. This is not an authorization mechanism.
"""
from __future__ import annotations

import re
from urllib.parse import urlencode


SITE_URL = "https://hatuli.ai-groundtruth.com"
_PUBLIC = frozenset({
    "/", "/example", "/how-it-works", "/guides/check-apartment-before-deposit",
    "/best-apartments", "/best-rentals", "/analytics", "/compare", "/map",
    "/complexes", "/developers", "/banks", "/mortgage-calculator", "/news", "/info",
    "/rent", "/rental/daily", "/rental/scanner", "/improve-listing",
})
_DETAIL = re.compile(r"/(?:analytics/\d{4}-\d{2}-\d{2}-(?:market|rent)(?:-local)?|(?:complex|developer|news)/[1-9]\d{0,18}|banks/[a-z][a-z0-9-]{0,63})\Z")
_SESSIONS = frozenset({"site_session", "admin_session", "admin_auth", "admin_user"})
_CAMPAIGN = frozenset({"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "yclid", "gclid", "fbclid", "msclkid"})
_FILTERS = {
    "/analytics": {"category": r"(?:market|rent)", "topic": r"(?:places|complexes|banks|developers|strategies)", "page": r"[1-9]\d{0,3}"},
    "/compare": {"hex": r"-?\d{1,6}:-?\d{1,6}", "complex": r"[1-9]\d{0,9}", "rooms": r"[1-6]",
                 "area": r"\d{1,3}(?:\.\d{1,3})?", "scope": r"(?:hex|ring1|ring2|ring4|ring8|city)",
                 "listing": r"(?:\d{0,20}|newbuild:\d{1,10})", "unit": r"[1-9]\d{0,9}"},
    "/rental/daily": {"rooms": r"[1-6]"},
    "/rental/scanner": {"section": r"candidates"},
    "/complexes": {"sort": r"(?:listings|name|price|rating)", "district": r"[\w -]{0,60}", "search": r"[\w .'-]{0,80}"},
    "/map": {"type": r"(?:sale|rental|complex|newbuild)", "year": r"20\d{2}", "rooms": r"[0-6]",
             "price_min": r"\d{1,10}", "price_max": r"\d{1,10}",
             "area_min": r"\d{1,4}(?:\.\d{1,2})?", "area_max": r"\d{1,4}(?:\.\d{1,2})?"},
}


def metrika_context(request, *, admin=False, site_tier=None, access_locked=None):
    """Return a minimal public URL, or None for any unreviewed context."""
    path = request.url.path
    # Playwright, local development and staging use the same templates. Their
    # visits must never reach the production counter, even with public paths.
    if request.url.hostname != "hatuli.ai-groundtruth.com":
        return None
    if request.method != "GET" or admin or (path not in _PUBLIC and not _DETAIL.fullmatch(path)):
        return None
    if any(name in request.cookies for name in _SESSIONS):
        return None
    if path == "/map" and site_tier != "public":
        return None
    if path == "/rental/scanner" and access_locked is not True:
        return None
    campaign = []
    seen = set()
    for key, value in request.query_params.multi_items():
        if key in seen:
            return None
        seen.add(key)
        if key == "_ym_debug" and value in {"1", "2"}:
            continue
        if key in _CAMPAIGN:
            if key.startswith("utm_"):
                valid = 1 <= len(value) <= 200 and all(char.isalnum() or char in " _.-" for char in value)
            else:
                valid = re.fullmatch(r"[A-Za-z0-9_.-]{1,512}", value) is not None
            if not valid:
                return None
            campaign.append((key, value))
        elif key not in _FILTERS.get(path, {}) or not re.fullmatch(_FILTERS[path][key], value):
            return None
    return {"url": SITE_URL + path + ("?" + urlencode(campaign) if campaign else "")}
