"""Public, ephemeral seller copy review with the existing listing access guard."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Request
import asyncpg

from bot.core.daily_picks import load_daily_picks
from bot.core.site_auth import get_user_tier
from bot.core.seller_listing_review import review_seller_listing, SellerListingReviewError

MAX_BODY_BYTES = 128 * 1024
PHOTO_KEYS = ("rooms", "kitchen", "bathroom", "floorplan", "windows", "entrance")
FIELDS = ("url", "title", "description", "rooms", "area", "price", "floor", "floors_total",
          "kitchen_area", "ceiling_height", "year_built", "address", "complex_name", "finish_level",
          "utilities_monthly", "utilities_period", "included_items", "viewing_terms", "photos_count",
          "is_owner", "legal_status")
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "same-origin"}
log = logging.getLogger(__name__)


def same_origin(request):
    value = request.headers.get("origin") or request.headers.get("referer")
    try:
        source = urlsplit(value or "")
        expected = urlsplit(str(request.base_url))
        def key(url):
            return url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80)
        return (source.scheme in ("http", "https") and not source.username and not source.password
                and key(source) == key(expected)
                and request.headers.get("sec-fetch-site", "same-origin") in ("same-origin", "none"))
    except ValueError:
        return False


async def read_fields(request):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise SellerListingReviewError("Объявление слишком большое. Сократите текст до 12 000 символов.")
        body.extend(chunk)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    try:
        if content_type == "application/json":
            def unique_fields(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            fields = json.loads(body, object_pairs_hook=unique_fields)
        elif content_type == "application/x-www-form-urlencoded":
            parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=40)
            if any(len(values) != 1 for values in parsed.values()):
                raise ValueError()
            fields = {key: values[0] for key, values in parsed.items()}
        else:
            raise ValueError()
        if (not isinstance(fields, dict) or len(fields) > 40
                or any(not isinstance(value, (str, int, float, bool)) for value in fields.values())):
            raise ValueError()
        return fields
    except (ValueError, UnicodeError, RecursionError):
        raise SellerListingReviewError("Не удалось прочитать форму. Заполните её ещё раз.") from None


def review_payload(fields):
    values = {key: fields[key] for key in FIELDS if key in fields and fields[key] != ""}
    if "is_owner" in values:
        if values["is_owner"] in ("true", True):
            values["is_owner"] = True
        elif values["is_owner"] in ("false", False):
            values["is_owner"] = False
        else:
            values.pop("is_owner")
    values["photo_coverage"] = {key: fields.get("photo_" + key, "unknown") for key in PHOTO_KEYS}
    return values


async def import_known_listing(request, values):
    """Never fetch a submitted URL or expose restricted descriptions anonymously."""
    if not values.get("url"):
        return values, None
    # Reuse the pure input validator before deriving an ID or running any SQL.
    checked = review_seller_listing({"url": values["url"]})
    canonical = checked.get("source_url")
    if not canonical:
        return values, None
    identifier = canonical.rsplit("/", 1)[-1]
    source = None
    snapshot = load_daily_picks()
    if snapshot:
        for item in snapshot.get("items", []):
            if item.get("id") != identifier:
                continue
            observed = datetime.fromisoformat(item["source_checked_at"].replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - timedelta(hours=72) <= observed <= datetime.now(timezone.utc):
                source = {key: item.get(key) for key in ("rooms", "area", "price", "floor", "floors_total", "complex_name")}
                source.update({key: value for key, value in (item.get("selection_facts") or {}).items()
                               if key in ("ceiling_height", "kitchen_area", "finish_level", "is_owner")})
            break
    try:
        tier = await get_user_tier(request)
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError):
        tier = "public"
        log.warning("Seller review session lookup unavailable")
    if tier in ("admin", "subscriber"):
        from bot.db.pg import fetchrow
        try:
            row = await fetchrow("""
                SELECT title,description,rooms,area,price,floor,floors_total,kitchen_area,
                       ceiling_height,year_built,address,complex_name,finish_level,is_owner
                FROM apartment_listings WHERE id=$1 AND is_active IS TRUE
                  AND last_seen >= now() - interval '72 hours'
            """, identifier)
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError):
            row = None
            log.warning("Seller review source lookup unavailable")
        if row:
            source = dict(row)
    if source:
        merged = {key: value for key, value in source.items() if key in FIELDS and value is not None}
        if merged.get("finish_level") in ("prefinished", "needs_renovation"):
            merged["finish_level"] = {"prefinished": "prefinish", "needs_renovation": "needs_repair"}[merged["finish_level"]]
        merged.update(values)  # Seller edits always win over the observed version.
        return merged, "Подставили известные параметры объявления. Проверьте их и добавьте актуальный текст и сведения о фото."
    return values, "По ссылке нет доступной свежей версии для разбора. Вставьте описание и отметьте, какие фото есть — рекомендации появятся сразу."


def make_seller_review_router(templates):
    router = APIRouter()

    def render(request, *, values=None, report=None, error=None, source_note=None, status=200):
        return templates.TemplateResponse(request=request, name="seller_review.html", context={
            "request": request, "values": values or {}, "report": report, "error": error,
            "source_note": source_note,
        }, status_code=status, headers=HEADERS)

    @router.get("/improve-listing")
    async def seller_review_page(request: Request):
        return render(request)

    @router.post("/improve-listing")
    async def seller_review_submit(request: Request):
        if not same_origin(request):
            return render(request, error="Обновите страницу и отправьте форму ещё раз.", status=403)
        fields = {}
        try:
            fields = await read_fields(request)
            values = review_payload(fields)
            if not any(values.get(key) is not None and values.get(key) != "" for key in FIELDS):
                raise SellerListingReviewError("Вставьте текст объявления, ссылку на Krisha или заполните параметры квартиры.")
            values, source_note = await import_known_listing(request, values)
            # A URL with no accessible source is not evidence about its text or
            # photos. Ask for content instead of diagnosing an empty cache.
            has_content = any(value is not None and value != "" and value != "unknown"
                              for key, value in values.items() if key not in ("url", "photo_coverage"))
            has_photos = any(value != "unknown" for value in values["photo_coverage"].values())
            report = review_seller_listing(values) if has_content or has_photos else None
            # Keep the source and user-entered facts editable, not in an external service.
            display = {key: values.get(key, "") for key in FIELDS}
            display.update({"photo_" + key: values["photo_coverage"].get(key, "unknown") for key in PHOTO_KEYS})
            if isinstance(display.get("is_owner"), bool):
                display["is_owner"] = "true" if display["is_owner"] else "false"
            return render(request, values=display, report=report, source_note=source_note)
        except SellerListingReviewError as exc:
            display = {key: fields.get(key, "") for key in (*FIELDS, *("photo_" + key for key in PHOTO_KEYS))}
            return render(request, values=display, error=str(exc), status=422)
    return router
