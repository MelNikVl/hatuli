"""Explicit rental periods and manually verified catalog provenance.

No amount, price per square metre or legacy record determines a period.
"""
from __future__ import annotations

import re
import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit

PERIODS = {"daily", "monthly", "unknown"}
# Normal-browser verification by the operator, 2026-10-15 22:36 UTC+5:
# selected monthly/daily switch, heading, and all first 23 cards agreed.
# This is evidence about these paths, not a claim of complete market coverage.
VERIFIED_CATALOG_PERIODS = {
    "/arenda/kvartiry/astana/": "monthly",
    "/arenda/kvartiry-posutochno/astana/": "daily",
}
_DAILY = re.compile(r"посуточ|(?:за|в|/)\s*(?:1\s*)?сут(?:ки|ок)?\b|за\s+ночь|per\s+(?:day|night)|\bdaily\b|тәулік", re.I)
_MONTHLY = re.compile(r"помесяч|(?:за|в|/)\s*(?:1\s*)?мес(?:яц|яца|яцев)?\b|per\s+month|\bmonthly\b|айына", re.I)
_HOURLY = re.compile(r"почас|(?:за|в|/)\s*(?:\d+\s*)?час|per\s+hour|сағат", re.I)


def catalog_period(path: str) -> str:
    parsed = urlsplit(path)
    if parsed.netloc not in ("", "krisha.kz") or parsed.scheme not in ("", "https") or parsed.fragment:
        return "unknown"
    # Pagination does not change the verified catalog. Unknown filters do.
    if any(key != "page" for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        return "unknown"
    return VERIFIED_CATALOG_PERIODS.get(parsed.path, "unknown")


def classify_rental_period(price_text="", title="", *, verified_catalog_period="unknown") -> str:
    text = " ".join(value for value in (price_text, title) if isinstance(value, str))
    daily, monthly, hourly = bool(_DAILY.search(text)), bool(_MONTHLY.search(text)), bool(_HOURLY.search(text))
    if hourly or daily and monthly:
        return "unknown"
    # A negative phrase is not positive daily/monthly evidence.
    if re.search(r"\bне\s+(?:сда[её]тся\s+)?(?:посуточ|помесяч)", text, re.I):
        return "unknown"
    explicit = "daily" if daily else "monthly" if monthly else "unknown"
    catalog = verified_catalog_period if verified_catalog_period in ("daily", "monthly") else "unknown"
    if explicit != "unknown" and catalog != "unknown" and explicit != catalog:
        return "unknown"
    return explicit if explicit != "unknown" else catalog


def rental_period_conflict(price_text="", title="", *, verified_catalog_period="unknown") -> bool:
    text = " ".join(value for value in (price_text, title) if isinstance(value, str))
    text = re.sub(r"\bне\s+(?:сда[её]тся\s+)?(?:посуточ\w*|помесяч\w*)", " ", text, flags=re.I)
    daily, monthly = bool(_DAILY.search(text)), bool(_MONTHLY.search(text))
    return bool(daily and monthly or daily and verified_catalog_period == "monthly"
                or monthly and verified_catalog_period == "daily")


def catalog_cursor_key(prop_type: str, path: str) -> str:
    if prop_type not in ("apartment", "parking", "commercial"):
        raise ValueError("Invalid rental property type")
    # Preserve the established monthly health/cursor namespace.
    period = catalog_period(path)
    suffix = "_DAILY" if period == "daily" else ""
    if prop_type == "apartment" and period == "unknown":
        suffix = "_UNKNOWN_"+hashlib.sha256(path.encode()).hexdigest()[:12]
    return f"RENTAL_CRAWL_V1_{prop_type.upper()}{suffix}"


def catalog_page_url(path: str, page: int) -> str:
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/arenda/"):
        raise ValueError("A local rental catalog path is required")
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "page"]
    if page > 1:
        query.append(("page", str(page)))
    return "https://krisha.kz"+parsed.path+("?"+urlencode(query) if query else "")
