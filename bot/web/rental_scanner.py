"""Free aggregate/manual scenarios, explicitly gated rental candidate access."""
from __future__ import annotations

import csv
from dataclasses import fields as dataclass_fields
import io
import json
import logging
import re
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from bot.core.site_auth import get_user_tier
from bot.core import sublease_market as market
from bot.core.sublease import (SubleaseError, SubleaseParams, number, params_from_inputs,
                               scenario_economics, sublease_economics, mid_term_economics, occupancy_scenarios)
from bot.db import settings

log = logging.getLogger(__name__)
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "same-origin", "X-Content-Type-Options": "nosniff"}
PARAM_FIELDS = {field.name for field in dataclass_fields(SubleaseParams)}
FILTER_FIELDS = {"strategy", "district", "complex_id", "hex_id", "rooms", "max_rent", "sort", "page"}
EXTRA_FIELDS = {"vacancy_fraction", "turnovers_per_year", "listing_id", "rent_month", "nightly", "furnishing"}
ALL_FIELDS = PARAM_FIELDS | FILTER_FIELDS | EXTRA_FIELDS


class _SearchTooBroad(SubleaseError):
    code = "narrower_filters_required"

    def __init__(self):
        super().__init__("filters", "Слишком широкий поиск. Уточните район, комнатность или максимальную аренду.")


def _settings():
    return {key: settings.get(key) for key in settings.DEFAULTS}


def _filters(values):
    if set(values) - ALL_FIELDS:
        raise SubleaseError("form", "Неизвестный параметр.")
    strategy = values.get("strategy", "daily")
    if strategy not in ("daily", "mid_term"):
        raise SubleaseError("strategy", "Выберите стратегию.")
    result = {"strategy": strategy, "rooms": int(number(values.get("rooms"), "rooms", 0, 10, integer=True)),
              "page": int(number(values.get("page", 1), "page", 1, 20, integer=True))}
    sort = values.get("sort", "breakeven")
    if sort not in ("breakeven", "margin", "startup"):
        raise SubleaseError("sort", "Неизвестная сортировка.")
    result["sort"] = sort
    district = values.get("district")
    if district not in (None, ""):
        if not isinstance(district, str) or not 1 <= len(district.strip()) <= 100 or any(ord(c) < 32 for c in district):
            raise SubleaseError("district", "Уточните район.")
        result["district"] = district.strip()
    for field in ("complex_id", "max_rent"):
        if values.get(field) not in (None, ""):
            result[field] = int(number(values[field], field, 1, 2_147_483_647 if field == "complex_id" else 2_000_000, integer=True))
    if values.get("hex_id") not in (None, ""):
        if not isinstance(values["hex_id"], str) or not re.fullmatch(r"-?[0-9]{1,6}:-?[0-9]{1,6}", values["hex_id"]):
            raise SubleaseError("hex_id", "Некорректный участок карты.")
        result["hex_id"] = values["hex_id"]
    return result


def _query(request):
    pairs = request.query_params.multi_items()
    if len(pairs) > 40 or len({key for key, _ in pairs}) != len(pairs):
        raise SubleaseError("form", "Параметры не должны повторяться.")
    return dict(pairs)


async def _body(request):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 16_384:
            raise SubleaseError("form", "Слишком большой запрос.")
        body.extend(chunk)
    try:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type == "application/json":
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            values = json.loads(body, object_pairs_hook=unique)
        elif content_type == "application/x-www-form-urlencoded":
            parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=40)
            if any(len(value) != 1 for value in parsed.values()):
                raise ValueError()
            values = {key: value[0] for key, value in parsed.items()}
        else:
            raise ValueError()
        if not isinstance(values, dict) or len(values) > 40 or set(values) - ALL_FIELDS:
            raise ValueError()
        return values
    except (ValueError, UnicodeError, RecursionError):
        raise SubleaseError("form", "Не удалось прочитать параметры.") from None


async def _paid(request):
    try:
        return await get_user_tier(request) in ("subscriber", "admin")
    except Exception as exc:
        log.warning("Rental scanner access unavailable (%s)", type(exc).__name__)
        return False


def _error(exc, status=422):
    result = {"errors": exc.errors}
    if getattr(exc, "code", None):
        result["error"] = exc.code
    return JSONResponse(result, status_code=status, headers=HEADERS)


def _locked():
    return JSONResponse({"error": "restricted", "access_locked": True,
                         "message": "Список квартир и CSV доступны с расширенным доступом."},
                        status_code=403, headers=HEADERS)


def _mapping(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return dict(value) if isinstance(value, dict) else None


def _economics(row, values, config):
    # A parser label cannot zero the furnishing budget until its own rules
    # version and the operator's real-source validation receipt agree.
    furnishing = row.get("furnishing") if market.furnished_row_ready(row, config) else "unknown"
    furnishing = furnishing or "unknown"
    params, sources = params_from_inputs(values, config, rooms=row["rooms"], furnishing=furnishing)
    index_kind = "daily_index" if values.get("strategy", "daily") == "daily" else "furnished_monthly_index"
    minimum_complex, minimum_area = market.thresholds(config)
    index = market.ready_index(_mapping(row.get(index_kind)), minimum_complex=minimum_complex, minimum_area=minimum_area)
    monthly = market.ready_index(_mapping(row.get("monthly_index")), minimum_complex=minimum_complex, minimum_area=minimum_area)
    if index is None or monthly is None:
        return None
    if (monthly["rent_period"] != "monthly" or monthly["furnishing"] is not None
            or index["rent_period"] != ("daily" if index_kind == "daily_index" else "monthly")
            or index["furnishing"] != (None if index_kind == "daily_index" else "full")):
        return None
    if index_kind == "daily_index":
        result = sublease_economics(row["price"], index["median_price"], furnishing, params)
        result["scenarios"] = scenario_economics(row["price"], index["median_price"], furnishing, params,
                                                 scenarios=occupancy_scenarios(config))
        result["conservative_price_scenario"] = sublease_economics(row["price"], index["p25_price"], furnishing, params)
    else:
        if not market.furnished_row_ready(_mapping(row.get("furnished_monthly_index")), config):
            return None
        result = mid_term_economics(row["price"], index["median_price"], furnishing, params,
                                   vacancy_fraction=values.get("vacancy_fraction"),
                                   turnovers_per_year=values.get("turnovers_per_year"))
    result["index_source"] = index
    result["parameter_sources"] = sources
    for scenario in result.get("scenarios", []):
        scenario["index_source"] = index
    if "conservative_price_scenario" in result:
        result["conservative_price_scenario"]["index_source"] = {**index, "price_basis": "p25"}
    return result


async def _scanner(values):
    filters, config = _filters(values), _settings()
    # Reject missing rates before querying private inventory.
    params_from_inputs(values, config, rooms=filters["rooms"], furnishing=values.get("furnishing", "unknown"))
    if filters["strategy"] == "mid_term":
        number(values.get("vacancy_fraction"), "vacancy_fraction", 0, 1)
        number(values.get("turnovers_per_year"), "turnovers_per_year", 0, 365)
    rows, truncated = await market.read_candidates(filters, config)
    if truncated:
        # A cheaper-rent preselection cannot establish the global optimum for
        # any financial sort. Reject before calculating or exporting a subset.
        raise _SearchTooBroad()
    items, missing = [], 0
    for row in rows:
        public = market.public_candidate(row)
        if public is None:
            continue
        try:
            result = _economics(row, values, config)
        except SubleaseError:
            missing += 1
            continue
        if result is None:
            missing += 1
            continue
        monthly = market.ready_index(_mapping(row.get("monthly_index")),
                                     minimum_complex=market.thresholds(config)[0], minimum_area=market.thresholds(config)[1])
        flags, unassessed = [], ["noise"]
        if row.get("has_photo") is not True:
            flags.append("no_photo")
        if result["furnishing"] not in ("none", "partial", "full"):
            flags.append("furnishing_unknown")
        if monthly and row["price"] <= monthly["median_price"] * .7:
            flags.append("unusually_low_asking_rent")
        supply = result["index_source"].get("active_supply")
        competition = row.get("competition_p75")
        if (type(supply) is int and isinstance(competition, (float, int))
                and not isinstance(competition, bool) and competition >= 0):
            if supply > competition:
                flags.append("high_daily_competition")
        else:
            unassessed.append("competition")
        # No external OSM request on a financial/API read. Noise stays unknown
        # until a coordinate-specific, evidenced saved measurement exists.
        public.update(economics=result, furnishing=result["furnishing"], risk_flags=flags,
                      unassessed_factors=unassessed, risk_assessment_complete=False)
        items.append(public)
    sort = filters["sort"]
    items.sort(key=lambda row: ((row["economics"]["breakeven_occupancy"]
                                if row["economics"]["breakeven_occupancy"] is not None else float("inf"))
                               if sort == "breakeven" else
                               -row["economics"]["margin"] if sort == "margin" else row["economics"]["startup_capital"], row["id"]))
    return items, filters, truncated, missing


def make_rental_scanner_router(templates):
    router = APIRouter()

    @router.get("/rental/scanner")
    async def scanner_page(request: Request):
        locked = not await _paid(request)
        if locked and request.query_params.get("section") == "candidates":
            return templates.TemplateResponse(request=request, name="access_locked.html", context={
                "request": request, "title": "Квартиры для субаренды",
                "message": "Калькулятор и цены сегмента открыты всем. Список квартир и CSV требуют расширенного доступа."},
                status_code=403, headers=HEADERS)
        return templates.TemplateResponse(request=request, name="rental_scanner.html", context={
            "request": request, "access_locked": locked, "parameter_names": sorted(PARAM_FIELDS)}, headers=HEADERS)

    @router.get("/api/rental/segment")
    async def segment(request: Request):
        try:
            values = _query(request)
            filters, config = _filters(values), _settings()
            segment_data = await market.read_segment(district=filters.get("district"), complex_id=filters.get("complex_id"),
                                                     rooms=filters["rooms"], settings=config)
            if filters.get("hex_id"):
                segment_data = await market.read_hex_segment(filters["hex_id"], filters["rooms"], config)
            economics, errors = None, {}
            if segment_data["monthly"] and segment_data["daily"]:
                try:
                    params, sources = params_from_inputs(values, config, rooms=filters["rooms"],
                                                          furnishing=values.get("furnishing", "unknown"))
                    economics = {"scenarios": scenario_economics(segment_data["monthly"]["median_price"],
                        segment_data["daily"]["median_price"], values.get("furnishing", "unknown"), params,
                        scenarios=occupancy_scenarios(config)),
                        "parameter_sources": sources, "index_source": segment_data["daily"]}
                    for row in economics["scenarios"]:
                        row["index_source"] = segment_data["daily"]
                except SubleaseError as exc:
                    errors = exc.errors
            return JSONResponse({"indices": segment_data, "economics": economics, "parameter_errors": errors,
                                 "price_kind": "asking", "occupancy_is_scenario": True,
                                 "removal_is_lease": False}, headers=HEADERS)
        except SubleaseError as exc:
            return _error(exc)
        except Exception as exc:
            log.warning("Rental segment unavailable (%s)", type(exc).__name__)
            return JSONResponse({"error": "unavailable"}, status_code=503, headers=HEADERS)

    @router.post("/api/rental/calc")
    async def calculate(request: Request):
        try:
            values, config = await _body(request), _settings()
            identifier = values.get("listing_id")
            if identifier not in (None, ""):
                if not isinstance(identifier, str) or not re.fullmatch(r"[0-9]{5,20}", identifier):
                    raise SubleaseError("listing_id", "Нужен идентификатор объявления аренды.")
                filters = _filters(values)
                rows, _ = await market.read_candidates(filters, config, listing_id=identifier, require_cheap=False)
                result = _economics(rows[0], values, config) if rows else None
                if result is None:
                    return JSONResponse({"error": "no_qualified_evidence"}, status_code=404, headers=HEADERS)
            else:
                furnishing = values.get("furnishing", "unknown")
                rooms = (int(number(values["rooms"], "rooms", 0, 10, integer=True))
                         if values.get("rooms") not in (None, "") else None)
                params, sources = params_from_inputs(values, config, rooms=rooms, furnishing=furnishing)
                if values.get("strategy", "daily") == "daily":
                    result = sublease_economics(values.get("rent_month"), values.get("nightly"), furnishing, params)
                    result["scenarios"] = scenario_economics(values.get("rent_month"), values.get("nightly"), furnishing, params,
                                                             scenarios=occupancy_scenarios(config))
                elif values.get("strategy") == "mid_term":
                    raise SubleaseError("strategy", "Для помесячной субаренды нужен подтверждённый индекс меблированных квартир.")
                else:
                    raise SubleaseError("strategy", "Выберите стратегию.")
                result["parameter_sources"] = sources
            return JSONResponse(result, headers=HEADERS)
        except SubleaseError as exc:
            return _error(exc)
        except Exception as exc:
            log.warning("Rental calculation unavailable (%s)", type(exc).__name__)
            return JSONResponse({"error": "unavailable"}, status_code=503, headers=HEADERS)

    @router.get("/api/rental/scanner")
    async def scanner(request: Request):
        if not await _paid(request):
            return _locked()
        try:
            items, filters, truncated, missing = await _scanner(_query(request))
            offset = (filters["page"] - 1) * 25
            return JSONResponse({"items": items[offset:offset+25], "page": filters["page"], "page_size": 25,
                                 "total": len(items), "pool_truncated": truncated, "unqualified_count": missing,
                                 "ranking_scope": "all_qualified_candidates_matching_filters", "access_locked": False}, headers=HEADERS)
        except SubleaseError as exc:
            return _error(exc)
        except Exception as exc:
            log.warning("Rental scanner unavailable (%s)", type(exc).__name__)
            return JSONResponse({"error": "unavailable"}, status_code=503, headers=HEADERS)

    @router.get("/api/rental/scanner.csv")
    async def scanner_csv(request: Request):
        if not await _paid(request):
            return _locked()
        try:
            items, _, truncated, _ = await _scanner(_query(request))
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(["id", "source_url", "district", "address", "rooms", "rent_month_kzt", "breakeven_occupancy",
                             "scenario_margin_kzt", "startup_capital_kzt", "index_n", "index_updated_at", "scenario_only",
                             *SubleaseParams.__dataclass_fields__, "index_level", "index_window_days", "index_source_oldest_at",
                             "index_source_newest_at", "index_policy_version"])
            def cell(value):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return value
                value = str(value if value is not None else "")
                return "'"+value if value[:1] in ("=", "+", "-", "@", "\t", "\r", "\n") else value
            for item in items:
                e = item["economics"]
                writer.writerow([cell(value) for value in (item["id"], item["source_url"], item["district"], item["address"], item["rooms"],
                    item["price"], e["breakeven_occupancy"], e["margin"], e["startup_capital"],
                    e["index_source"]["n"], e["index_source"]["updated_at"], True,
                    *(e["params_used"].get(key) for key in SubleaseParams.__dataclass_fields__),
                    *(e["index_source"].get(key) for key in ("level", "window_days", "source_oldest_at", "source_newest_at", "policy_version")))])
            return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers={**HEADERS,
                "Content-Disposition": 'attachment; filename="hatuli-rental-scenarios.csv"',
                "X-Hatuli-Pool-Truncated": str(truncated).lower()})
        except SubleaseError as exc:
            return _error(exc)
        except Exception as exc:
            log.warning("Rental CSV unavailable (%s)", type(exc).__name__)
            return JSONResponse({"error": "unavailable"}, status_code=503, headers=HEADERS)

    return router
