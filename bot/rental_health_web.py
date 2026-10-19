"""Protected rental collection health: aggregate counts and allowlisted state."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from bot.core.admin_sessions import is_admin
from bot.core.rental_service_metrics import COUNTERS
from bot.core.rental_period import catalog_cursor_key
from bot.db.pg import fetch

log = logging.getLogger(__name__)
HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}
TYPES = {"apartment": "Квартиры", "parking": "Парковки и гаражи", "commercial": "Коммерция"}
CATALOGS = (
    ("apartment", "monthly", "Квартиры · помесячно", "/arenda/kvartiry/astana/"),
    ("apartment", "daily", "Квартиры · посуточно", "/arenda/kvartiry-posutochno/astana/"),
    ("parking", "unknown", TYPES["parking"], "/arenda/garazhi/astana/"),
    ("commercial", "unknown", TYPES["commercial"], "/arenda/kommercheskaya-nedvizhimost/astana/"),
)
PERIOD_LABELS = {"monthly": "Помесячная", "daily": "Посуточная", "unknown": "Срок не подтверждён", "conflict": "Противоречивый срок"}
SUFFIXES = (
    "STARTED_AT", "COMPLETED_AT", "COMPLETED_VERIFIED", "DURATION_SEC", "LAST_PAGES", "UNIQUE_SEEN", "TOTAL",
    "LAST_STATUS", "LAST_SUCCESS_AT", "ABORTED_AT", "ABORT_REASON",
    "INCOMPLETE_COMPLETED_AT", "EXPECTED_LAST_PAGE", "EXPECTED_TOTAL",
    "PAGE_INTERVAL_SEC", "PAGE_INTERVAL_SAMPLES", "ESTIMATED_HOURS",
)
STAT_FIELDS = (
    "total_rows", "active_rows", "clean_active_rows", "fresh_72h", "fresh_1h", "stale_active",
    "new_24h", "updated_24h", "confirmed_archives_24h", "status_mismatches",
    "with_complex_source_link", "with_address", "with_area", "with_rooms", "with_floor",
    "with_coordinates", "with_photos", "with_gallery", "catalog_fresh_72h", "catalog_fresh_1h",
    "detail_fresh_24h", "detail_fresh_72h", "detail_missing", "detail_stale_72h",
    "detail_attempted_24h",
)

_CLEAN = ("COALESCE(is_duplicate, FALSE) = FALSE AND COALESCE(dup_needs_review, FALSE) = FALSE "
          "AND NULLIF(btrim(duplicate_of), '') IS NULL")
_LIVE = f"is_active IS TRUE AND archived_at IS NULL AND {_CLEAN}"
_FRESH = f"{_LIVE} AND last_seen BETWEEN now() - interval '72 hours' AND now()"
AGGREGATE_SQL = f"""
    WITH scoped AS (
        SELECT prop_type, is_active, archived_at, is_duplicate, dup_needs_review,
            duplicate_of, last_seen, found_at, archive_reason, complex_name, complex_url,
            address, area, rooms, floor, lat, lon, photo_url, photos, rental_episode_last_seen_at,
            description_checked_at, coord_fetch_attempted_at, CASE
            WHEN prop_type = 'apartment' AND COALESCE(rent_period_conflict, FALSE) THEN 'conflict'
            WHEN prop_type = 'apartment' AND rent_period IN ('monthly', 'daily') THEN rent_period
            ELSE 'unknown' END AS scope_period
        FROM rental_listings
        WHERE city = 'astana' AND prop_type = ANY($1::text[])
    )
    SELECT prop_type, scope_period AS rent_period, count(*) AS total_rows,
        count(*) FILTER (WHERE is_active IS TRUE AND archived_at IS NULL) AS active_rows,
        count(*) FILTER (WHERE {_LIVE}) AS clean_active_rows,
        count(*) FILTER (WHERE {_FRESH}) AS fresh_72h,
        count(*) FILTER (WHERE {_LIVE} AND last_seen BETWEEN now() - interval '1 hour' AND now()) AS fresh_1h,
        count(*) FILTER (WHERE {_LIVE} AND (last_seen IS NULL OR last_seen < now() - interval '72 hours'
                                           OR last_seen > now())) AS stale_active,
        count(*) FILTER (WHERE {_CLEAN} AND found_at BETWEEN now() - interval '24 hours' AND now()) AS new_24h,
        count(*) FILTER (WHERE {_CLEAN} AND found_at < now() - interval '24 hours'
                         AND last_seen BETWEEN now() - interval '24 hours' AND now()) AS updated_24h,
        count(*) FILTER (WHERE {_CLEAN} AND is_active IS FALSE
                         AND archive_reason IN ('archived_badge', 'confirmed_gone')
                         AND archived_at BETWEEN now() - interval '24 hours' AND now()) AS confirmed_archives_24h,
        count(*) FILTER (WHERE (is_active IS TRUE AND archived_at IS NOT NULL)
                         OR (is_active IS FALSE AND archived_at IS NOT NULL AND last_seen > archived_at)) AS status_mismatches,
        count(*) FILTER (WHERE {_FRESH} AND NULLIF(btrim(complex_name), '') IS NOT NULL
                         AND complex_url ~ '^https?://(www\\.)?krisha\\.kz/complex/show/[^?#[:space:]]+($|[?#])') AS with_complex_source_link,
        count(*) FILTER (WHERE {_FRESH} AND NULLIF(btrim(address), '') IS NOT NULL) AS with_address,
        count(*) FILTER (WHERE {_FRESH} AND area > 0) AS with_area,
        count(*) FILTER (WHERE {_FRESH} AND rooms >= 0) AS with_rooms,
        count(*) FILTER (WHERE {_FRESH} AND floor > 0) AS with_floor,
        count(*) FILTER (WHERE {_FRESH} AND lat BETWEEN 50.5 AND 52 AND lon BETWEEN 70.5 AND 72.5) AS with_coordinates,
        count(*) FILTER (WHERE {_FRESH} AND (NULLIF(btrim(photo_url), '') IS NOT NULL
                         OR (jsonb_typeof(photos) = 'array' AND photos <> '[]'::jsonb))) AS with_photos,
        count(*) FILTER (WHERE {_FRESH} AND jsonb_typeof(photos) = 'array'
                         AND photos <> '[]'::jsonb) AS with_gallery,
        count(*) FILTER (WHERE {_LIVE} AND scope_period IN ('monthly', 'daily')
                         AND rental_episode_last_seen_at BETWEEN now() - interval '72 hours' AND now()) AS catalog_fresh_72h,
        count(*) FILTER (WHERE {_LIVE} AND scope_period IN ('monthly', 'daily')
                         AND rental_episode_last_seen_at BETWEEN now() - interval '1 hour' AND now()) AS catalog_fresh_1h,
        count(*) FILTER (WHERE {_LIVE} AND description_checked_at
                         BETWEEN now() - interval '24 hours' AND now()) AS detail_fresh_24h,
        count(*) FILTER (WHERE {_LIVE} AND description_checked_at
                         BETWEEN now() - interval '72 hours' AND now()) AS detail_fresh_72h,
        count(*) FILTER (WHERE {_LIVE} AND description_checked_at IS NULL) AS detail_missing,
        count(*) FILTER (WHERE {_LIVE} AND (description_checked_at IS NULL
                         OR description_checked_at < now() - interval '72 hours'
                         OR description_checked_at > now())) AS detail_stale_72h,
        count(*) FILTER (WHERE {_LIVE} AND coord_fetch_attempted_at
                         BETWEEN now() - interval '24 hours' AND now()) AS detail_attempted_24h,
        max(description_checked_at) FILTER (WHERE {_LIVE} AND description_checked_at <= now()) AS last_detail_success_at,
        max(rental_episode_last_seen_at) FILTER (WHERE {_LIVE} AND scope_period IN ('monthly', 'daily')
                         AND rental_episode_last_seen_at <= now()) AS last_catalog_observed_at,
        max(last_seen) FILTER (WHERE {_LIVE} AND last_seen <= now()) AS last_observed_at
    FROM scoped
    GROUP BY prop_type, scope_period
"""


def _number(value, *, integer=False, maximum=10**12):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, bool) or not math.isfinite(number) or not 0 <= number <= maximum:
        return None
    return int(number) if integer and number.is_integer() else (None if integer else round(number, 3))


def _date(value) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value.astimezone(timezone.utc) if isinstance(value, datetime) and value.tzinfo else None


def _iso(value) -> str | None:
    parsed = _date(value)
    return parsed.isoformat() if parsed else None


def _object(value, *, maximum_size=1_000_000) -> dict:
    if not isinstance(value, str) or len(value) > maximum_size:
        return {}
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_cursor(value) -> dict:
    raw = _object(value)
    ids = raw.get("seen_ids")
    page = _number(raw.get("page"), integer=True, maximum=100_000)
    last_page = _number(raw.get("expected_last_page"), integer=True, maximum=100_000)
    return {"next_page": page if page else None,
            "pages_seen": _number(raw.get("pages_seen"), integer=True, maximum=100_000),
            "unique_seen": len({item for item in ids if isinstance(item, str) and item.isdigit()}) if isinstance(ids, list) else None,
            "started_from_first": raw.get("started_from_first") is True,
            "expected_last_page": last_page if last_page else None,
            "expected_total": _number(raw.get("expected_total"), integer=True)}


def _safe_cycle(value) -> dict | None:
    raw = _object(value, maximum_size=16_000)
    if not raw:
        return None
    stages = raw.get("stages")
    allowed_stages = ("index", "sheets", "dedup", "address_bind", "geo_bind", "backfill", "archive")
    statuses = ("ok", "error", "timeout", "budget_exhausted")
    output = {"at": _iso(raw.get("at")),
              "type": raw.get("type") if isinstance(raw.get("type"), str) and raw.get("type") in TYPES else None,
              "list_status": raw.get("list_status") if isinstance(raw.get("list_status"), str) and raw.get("list_status") in ("ok", "error", "blocked") else None,
              "geo_complex_guess_enabled": raw.get("geo_complex_guess_enabled") is True,
              "stages": {}}
    catalog = next((item for item in CATALOGS
                    if item[0] == output["type"]
                    and raw.get("scope") == catalog_cursor_key(item[0], item[3]).removeprefix("RENTAL_CRAWL_V1_").lower()
                    and raw.get("rent_period") == item[1]), None)
    output["scope_label"] = catalog[2] if catalog else None
    output["rent_period"] = catalog[1] if catalog else None
    for key in ("cycle", "page", "saved", "head_pages", "durable_pages", "page_interval_samples"):
        output[key] = _number(raw.get(key), integer=True)
    for key in ("list_seconds", "maintenance_seconds", "work_seconds", "sleep_seconds",
                "page_interval_seconds", "estimated_circle_hours"):
        output[key] = _number(raw.get(key))
    index = raw.get("index")
    if isinstance(index, dict):
        reasons = ("startup", "apartment_pages", "saved_observations_age", "full_pass", "periodic_revalidation")
        output["index"] = {"due_reason": index.get("due_reason")
                           if isinstance(index.get("due_reason"), str) and index.get("due_reason") in reasons else None}
        for key in ("pending_observations", "apartment_pages_since_refresh", "failures"):
            output["index"][key] = _number(index.get(key), integer=True)
        for key in ("refresh_interval_seconds", "dirty_age_seconds", "last_success_age_seconds", "retry_after_seconds"):
            output["index"][key] = _number(index.get(key))
    if isinstance(stages, dict):
        for key in allowed_stages:
            stage = stages.get(key)
            if isinstance(stage, dict):
                output["stages"][key] = {"seconds": _number(stage.get("seconds")),
                    "status": stage.get("status") if isinstance(stage.get("status"), str) and stage.get("status") in statuses else "unknown"}
    detail = raw.get("detail")
    if isinstance(detail, dict):
        allowed_statuses = ("ok", "success", "idle", "cooldown", "blocked", "error", "timeout",
                            "budget_exhausted", "cycle_stopped", "partial", "no_candidates")
        scopes = {catalog_cursor_key(item[0], item[3]).removeprefix("RENTAL_CRAWL_V1_").lower(): item[2]
                  for item in CATALOGS}
        scope = detail.get("queue_scope")
        status = detail.get("status")
        output["detail"] = {
            "scope_label": scopes.get(scope) if isinstance(scope, str) else None,
            "status": status if isinstance(status, str) and status in allowed_statuses else "unknown",
        }
        for key in ("selected", "checked", "success", "detail_attempts", "requests", "blocked", "errors"):
            output["detail"][key] = _number(detail.get(key), integer=True)
        for key in ("cooldown_remaining_seconds", "oldest_due_age_hours"):
            output["detail"][key] = _number(detail.get(key))
        output["detail"]["budget_exhausted"] = detail.get("budget_exhausted") is True
    return output


def project_rental_health(rows, settings: dict, *, now: datetime | None = None) -> dict:
    """Projection is deliberately independent of raw cursor IDs/settings."""
    now = now or datetime.now(timezone.utc)
    by_period = {(row["prop_type"], row.get("rent_period")): row for row in rows
                 if row.get("prop_type") in TYPES and row.get("rent_period") in PERIOD_LABELS}
    types = []
    for prop_type, period, label, path in CATALOGS:
        row = by_period.get((prop_type, period), {})
        prefix = catalog_cursor_key(prop_type, path)
        cursor = _safe_cursor(settings.get(prefix))
        cursor["expected_last_page"] = cursor["expected_last_page"] or _number(
            settings.get(prefix + "_EXPECTED_LAST_PAGE"), integer=True, maximum=100_000)
        if cursor["expected_total"] is None:
            cursor["expected_total"] = _number(settings.get(prefix + "_EXPECTED_TOTAL"), integer=True)
        completed = _date(settings.get(prefix + "_COMPLETED_AT"))
        duration = _number(settings.get(prefix + "_DURATION_SEC"))
        completed_pages = _number(settings.get(prefix + "_LAST_PAGES"), integer=True)
        completed_unique = _number(settings.get(prefix + "_UNIQUE_SEEN"), integer=True)
        completed_total = _number(settings.get(prefix + "_TOTAL"), integer=True)
        # The old collector stored duration/page but no unique coverage proof.
        verified = bool(settings.get(prefix + "_COMPLETED_VERIFIED") == "1"
                        and completed and completed <= now and completed_pages and
                        completed_unique and completed_total and completed_unique >= completed_total * .95
                        and duration is not None and duration > 0)
        success = _date(settings.get(prefix + "_LAST_SUCCESS_AT"))
        observed = _date(row.get("last_observed_at"))
        recent = bool(success and timedelta(0) <= now - success <= timedelta(minutes=30))
        status = "collecting" if recent else ("stale" if success or observed else "unknown")
        raw_status = settings.get(prefix + "_LAST_STATUS")
        item = {"type": prop_type, "rent_period": period, "scope": prefix.removeprefix("RENTAL_CRAWL_V1_").lower(), "label": label, "status": status,
                "last_success_at": _iso(success), "last_observed_at": _iso(observed),
                "last_catalog_observed_at": _iso(row.get("last_catalog_observed_at")),
                "cursor": cursor,
                "last_round_status": raw_status if raw_status in (
                    "complete", "collecting", "incomplete_legacy_tail") else "unknown",
                "last_completed_at": _iso(completed), "last_completion_verified": verified,
                "last_circle_hours": round(duration / 3600, 2) if verified else None,
                "last_circle_pages": completed_pages if verified else None,
                "last_circle_unique_seen": completed_unique if verified else None,
                "last_circle_source_total": completed_total if verified else None,
                "estimated_circle_hours": _number(settings.get(prefix + "_ESTIMATED_HOURS")) if recent else None,
                "page_interval_seconds": _number(settings.get(prefix + "_PAGE_INTERVAL_SEC")) if recent else None,
                "page_interval_samples": _number(settings.get(prefix + "_PAGE_INTERVAL_SAMPLES"), integer=True) if recent else None,
                "aborted_at": _iso(settings.get(prefix + "_ABORTED_AT")),
                "incomplete_completed_at": _iso(settings.get(prefix + "_INCOMPLETE_COMPLETED_AT"))}
        item.update({key: _number(row.get(key, 0), integer=True) or 0 for key in STAT_FIELDS})
        item["last_detail_success_at"] = _iso(row.get("last_detail_success_at"))
        item["detail_fresh_share_pct"] = round(item["detail_fresh_72h"] / item["clean_active_rows"] * 100, 2) if item["clean_active_rows"] else None
        # A busy cursor or optimistic ETA is not proof that a complete day met
        # the user's 24-hour target. A verified completed round is required.
        item["target_circle_hours"] = 24
        item["last_round_within_target"] = bool(verified and completed
            and timedelta(0) <= now-completed <= timedelta(hours=24) and duration <= 24*3600)
        item["estimated_target_missed"] = bool(item["estimated_circle_hours"] is not None
                                              and item["estimated_circle_hours"] > 24)
        types.append(item)
    periods = []
    for period, label in PERIOD_LABELS.items():
        row = by_period.get(("apartment", period), {})
        periods.append({"rent_period": period, "label": label, **{
            key: _number(row.get(key, 0), integer=True) or 0
            for key in ("clean_active_rows", "fresh_72h", "catalog_fresh_72h", "detail_fresh_72h", "detail_stale_72h")}})
    day = now.astimezone(timezone(timedelta(hours=5))).date()
    daily = []
    for offset in range(3):
        current = (day - timedelta(days=offset)).isoformat()
        raw = _object(settings.get("RENTAL_METRICS_V1_" + current), maximum_size=8000)
        counter = {key: _number(raw.get(key, 0), integer=True) or 0 for key in COUNTERS}
        requests = sum(counter[key] for key in ("list_requests", "detail_requests", "archive_requests"))
        daily.append({"day": current, "available": bool(raw), "counters": counter,
                      "http_468_share_pct": round(counter["http_468"] / requests * 100, 2) if requests else None})
    revision = settings.get("RENTAL_SERVICE_REVISION", "")
    return {"at": now.isoformat(), "market_coverage": "unknown",
            "scope": "Астана · помесячная и посуточная аренда квартир, парковки/гаражи, коммерция",
            "notes": [
                "Полнота рынка пока не подтверждена: даже полный обход динамической выдачи может пропустить объявления.",
                "Помесячный и посуточный каталоги квартир обходятся отдельно, со своими курсорами и временем круга. Дома и комнаты в этот охват не входят. Объявления с неизвестным или противоречивым сроком не входят в арендные индексы.",
                "Архив означает подтверждённое снятие объявления, а не подтверждённую сдачу квартиры.",
                "Свежий чистый пул: активные объявления без дублей/спорных дублей с last_seen за 72 часа. Эта отметка сама по себе не подтверждает повторный обход проверенной категории срока аренды; для квартир отдельно считаем подтверждения по rental_episode_last_seen_at. Ссылка на ЖК — сохранённый сигнал источника; она сама по себе не доказывает точную привязку к корпусу. Привязку по близости на карте не считаем подтверждением.",
                "Наличие фото включает обложку из списка. Сохранённая галерея и параметры из деталей показываются отдельно; содержимое фотографий автоматически здесь не проверяется.",
                "Свежесть полной страницы считается по description_checked_at отдельно от выдачи. Ошибка, блокировка и локальный пересчёт не обновляют эту дату. Отметка попытки не доказывает успешное обновление характеристик.",
                "Суточная цель — подтверждённый обход каталога за 24 часа, а не расчётная скорость курсора. При блокировке источника цель может не выполняться; прежние даты и пропуски сохраняются.",
                "Счётчики сервиса считают наблюдения при сборе; обновления одной квартиры могут повторяться. Статистика БД за 24 часа считает строки, а не запросы.",
            ], "types": types, "apartment_periods": periods, "daily": daily,
            "latest_cycle": _safe_cycle(settings.get("RENTAL_SERVICE_CYCLE_LAST")),
            "service_revision": revision if isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{7,40}", revision) else "unknown",
            "service_started_at": _iso(settings.get("RENTAL_SERVICE_STARTED_AT"))}


async def load_rental_health(*, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    keys = ["RENTAL_SERVICE_REVISION", "RENTAL_SERVICE_STARTED_AT", "RENTAL_SERVICE_CYCLE_LAST"]
    for prop_type, _period, _label, path in CATALOGS:
        prefix = catalog_cursor_key(prop_type, path)
        keys.extend([prefix, *(prefix + "_" + suffix for suffix in SUFFIXES)])
    day = now.astimezone(timezone(timedelta(hours=5))).date()
    keys.extend("RENTAL_METRICS_V1_" + (day - timedelta(days=offset)).isoformat() for offset in range(3))
    rows, settings = await asyncio.gather(
        fetch(AGGREGATE_SQL, list(TYPES)),
        fetch("SELECT key, value FROM app_settings WHERE key = ANY($1::text[])", keys))
    return project_rental_health([dict(row) for row in rows],
                                 {row["key"]: row["value"] for row in settings}, now=now)


def register_rental_health_routes(app, templates) -> None:
    @app.get("/admin/api/rental-health")
    async def rental_health_api(request: Request):
        if not is_admin(request):
            return JSONResponse({"error": "Forbidden"}, status_code=403, headers=HEADERS)
        try:
            return JSONResponse(await load_rental_health(), headers=HEADERS)
        except Exception:
            log.exception("Rental health could not be loaded")
            return JSONResponse({"error": "Данные мониторинга временно недоступны"}, status_code=503, headers=HEADERS)

    @app.get("/admin/rental-health", response_class=HTMLResponse)
    async def rental_health_page(request: Request):
        if not is_admin(request):
            return RedirectResponse("/admin/login", status_code=302, headers=HEADERS)
        try:
            health = await load_rental_health()
        except Exception:
            log.exception("Rental health page could not be loaded")
            return HTMLResponse("Данные мониторинга временно недоступны", status_code=503, headers=HEADERS)
        return templates.TemplateResponse(request=request, name="admin_rental_health.html",
                                          context={"request": request, "health": health}, headers=HEADERS)
