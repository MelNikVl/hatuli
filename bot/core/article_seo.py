"""Deterministic metadata from already published editorial text.

Google has no fixed title/description character limit. Keep complete editorial
statements, including numbers and qualifications, rather than clipping a price
or losing a negation to satisfy an arbitrary snippet length. The publisher
already bounds title/summary/lead at 180/400/1000 characters respectively.
This helper neither writes archives nor invents a market observation date.
"""
from __future__ import annotations

from datetime import date
from html import unescape
import re


def _text(value: object, *, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        return ""
    # Decode editorial entities once; Jinja escapes the resulting plain text
    # in each HTML context. Numeric whitespace (including nbsp) stays a space.
    value = unescape(value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    return " ".join(value.split())


def _edition_label(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return ""
    try:
        return date.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return ""


def article_seo(article: dict) -> dict[str, str]:
    """Return stable, server-rendered tags without manual SEO fields.

    Publication-edition dates distinguish daily articles even when their H1
    repeats. A revised article retains its original edition date. No archive
    scan, clock, database, network request, or inferred location is required.
    """
    headline = _text(article.get("title"), limit=1000)
    category = _text(article.get("category_label"), limit=200)
    # These fallbacks describe only existing content. They do not promise
    # prices, district recommendations, yields, or data that may be absent.
    heading = headline or category or "Аналитика"
    edition = _edition_label(article.get("edition_date"))
    title = heading + (f" · {edition}" if edition else "") + " — Hatuli"
    description = (_text(article.get("summary"), limit=2000)
                   or _text(article.get("lead"), limit=2000)
                   or headline or category)
    if edition:
        if description and description[-1] not in ".!?…":
            description += "."
        description += (" " if description else "") + f"Выпуск от {edition}."
    return {"title": title, "description": description}
