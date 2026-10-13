"""Rental-only private detail API; a rental ID never falls back to a sale."""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone

from fastapi import Path, Request
from fastapi.responses import JSONResponse

from bot.core.site_auth import get_user_tier
from bot.core.rental_parser import _rental_card_photo_url
from bot.db.pg import fetch, fetchrow

log = logging.getLogger(__name__)
HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}
DETAIL_SQL = """
    SELECT id, title, price, area, rooms, floor, floors_total, address, district,
           complex_name, prop_type, photos, photo_url, last_seen, found_at,
           is_active, archived_at, archive_reason
    FROM rental_listings WHERE id = $1
"""
HISTORY_SQL = """
    SELECT old_price, new_price, changed_at FROM rental_price_history
    WHERE listing_id = $1 ORDER BY changed_at DESC, id DESC LIMIT 101
"""


def _number(value, low, high, *, integer=False):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value) or not low <= value <= high:
        return None
    return int(value) if integer and value.is_integer() else (None if integer else value)


def _timestamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def project_rental_detail(row: dict, history: list, *, history_available=True, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    identifier = str(row["id"])
    raw_photos = row.get("photos")
    if isinstance(raw_photos, str):
        try:
            raw_photos = json.loads(raw_photos)
        except ValueError:
            raw_photos = []
    candidates = (raw_photos if isinstance(raw_photos, list) else []) + [row.get("photo_url")]
    photos = []
    for candidate in candidates[:30]:
        safe = _rental_card_photo_url(candidate)
        if safe and safe not in photos:
            photos.append(safe)
    observed = row.get("last_seen")
    recent = bool(isinstance(observed, datetime) and observed.tzinfo
                  and timedelta(0) <= now - observed <= timedelta(hours=72))
    if row.get("is_active") is False:
        confirmed = row.get("archive_reason") in ("archived_badge", "confirmed_gone")
        status = "confirmed_removed" if confirmed else "unconfirmed_inactive"
    elif row.get("is_active") is True and row.get("archived_at") is None and recent:
        status = "active_observed"
    else:
        status = "unknown"
    labels = {"confirmed_removed": "Объявление снято с публикации",
              "unconfirmed_inactive": "Неактивно в нашей базе; снятие источником не подтверждено",
              "active_observed": "Замечено в списке за последние 72 часа",
              "unknown": "Актуальность требует проверки"}
    kind = row.get("prop_type") if row.get("prop_type") in ("apartment", "parking", "commercial") else "unknown"
    return {"id": identifier, "source_url": "https://krisha.kz/a/show/" + identifier,
            "title": str(row.get("title") or "Объявление об аренде")[:500],
            "price": _number(row.get("price"), 1, 2_147_483_647, integer=True),
            "area": _number(row.get("area"), .1, 1_000_000),
            "rooms": _number(row.get("rooms"), 0, 20, integer=True),
            "floor": _number(row.get("floor"), 1, 100, integer=True),
            "floors_total": _number(row.get("floors_total"), 1, 100, integer=True),
            "address": str(row.get("address") or "")[:600],
            "district": str(row.get("district") or "")[:100],
            "complex": str(row.get("complex_name") or "")[:200],
            "prop_type": kind, "monthly_category": kind == "apartment",
            "photos": photos[:20], "last_seen": _timestamp(observed), "found_at": _timestamp(row.get("found_at")),
            "status": status, "status_label": labels[status],
            "archive_note": "Снятие объявления не подтверждает сдачу квартиры.",
            "history_available": bool(history_available), "history_truncated": len(history) > 100,
            "price_history": [{"old_price": _number(item.get("old_price"), 1, 2_147_483_647, integer=True),
                               "new_price": _number(item.get("new_price"), 1, 2_147_483_647, integer=True),
                               "changed_at": _timestamp(item.get("changed_at"))} for item in history[:100]]}


def register_rental_detail_routes(app) -> None:
    @app.get("/admin/api/rental/{listing_id}")
    async def rental_detail(request: Request, listing_id: str = Path(pattern=r"^\d{5,20}$")):
        try:
            tier = await get_user_tier(request)
        except Exception:
            tier = "public"
        if tier not in ("admin", "subscriber"):
            return JSONResponse({"error": "restricted", "message": "Войдите и запросите доступ к объявлениям."},
                                status_code=403, headers=HEADERS)
        try:
            row = await fetchrow(DETAIL_SQL, listing_id)
            if row is None:
                return JSONResponse({"error": "not_found"}, status_code=404, headers=HEADERS)
            history_available = True
            try:
                history = await fetch(HISTORY_SQL, listing_id)
            except Exception as exc:
                log.warning("Rental price history unavailable (%s)", type(exc).__name__)
                history, history_available = [], False
            return JSONResponse(project_rental_detail(dict(row), [dict(item) for item in history],
                                                      history_available=history_available), headers=HEADERS)
        except Exception as exc:
            log.warning("Rental detail unavailable (%s)", type(exc).__name__)
            return JSONResponse({"error": "unavailable", "message": "Не удалось загрузить объявление."},
                                status_code=503, headers=HEADERS)
