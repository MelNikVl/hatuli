"""Anonymous comparison pages read published files, never live/private databases."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from statistics import median
from urllib.parse import urlencode

from fastapi import Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from bot.core.daily_picks import load_daily_picks, sanitized_listing_address, snapshot_status
from bot.core.deposit_comparison import compare_investment, load_deposit_catalog, public_deposit_catalog
from bot.core.market_comparison import (
    LOCAL_TIMEZONE, SCOPE_RADII, compare_market, hex_distance,
    load_market_snapshot, public_primary_offer, valid_hex,
)
from bot.core.web_pages import not_found_page
from bot.core.price_forecast_runtime import forecast_for_offer
from bot.core.comparison_offer_details import DETAIL_FIELDS, details_for_selected_offer, source_characteristics_changed
from bot.core.apartment_selection_reasons import apartment_selection_reasons
from bot.core.comparison_rental_estimate import validate_rental_estimate


def _public_offer(offer: dict) -> dict:
    primary = offer.get("market_type") == "primary"
    wait = 0 if not primary or offer.get("build_status") == "commissioned" else 12
    identifier = offer["id"]
    params = {"unit": identifier.removeprefix("newbuild:")} if primary else {"listing": identifier}
    title = f"{offer['rooms']}-комнатная · {offer['area']:g} м²".replace(".", ",")
    cautions = list(offer.get("cautions", []))
    if primary and wait:
        cautions.append("Для неподтверждённой готовности выбран сценарий ожидания 12 месяцев. Уточните срок ключей и измените его в расчёте.")
    observed = offer.get("source_checked_at") or offer.get("observed_at")
    observed_label = datetime.fromisoformat(observed.replace("Z", "+00:00")).astimezone(LOCAL_TIMEZONE).strftime("%d.%m.%Y, %H:%M") if observed else None
    return {
        "id": identifier, "market_type": "primary" if primary else "secondary", "title": title,
        "complex_name": offer.get("complex_name"), "rooms": offer["rooms"], "area": offer["area"],
        "price": offer["price"], "url": f"/listing/nb-{identifier.removeprefix('newbuild:')}" if primary else offer.get("url") or offer.get("source_url"),
        "href": "/compare?" + urlencode(params) + "#investment", "wait_months": wait,
        "finish_label": offer.get("finish_label", "Отделку и состояние нужно уточнить"),
        "observed_at": offer.get("observed_at"), "source_label": offer.get("source_label", "Krisha.kz"),
        "observed_at_label": observed_label, "build_status_label": offer.get("build_status_label"),
        "cautions": cautions,
        "reasons": apartment_selection_reasons(offer),
        **{field: offer.get(field) for field in DETAIL_FIELDS},
        "rental_estimate": validate_rental_estimate(offer.get("rental_estimate")),
    }


def _primary_selection_offer(offer, market):
    """Compare developer units within their own project, rooms and area band."""
    complex_id = offer.get("complex_id")
    if not isinstance(complex_id, int) or isinstance(complex_id, bool) or complex_id <= 0:
        # Missing project IDs cannot group unrelated, unidentified buildings.
        return _public_offer({**offer, "price_comparison": None})
    peers = [row["price_per_m2"] for row in market.get("offers", [])
             if row["market_type"] == "primary" and row.get("complex_id") == complex_id
             and row["id"] != offer["id"] and row["rooms"] == offer["rooms"]
             and offer["area"] * .85 <= row["area"] <= offer["area"] * 1.15]
    generated = market.get("generated_at")
    fresh = generated and 0 <= (datetime.now(timezone.utc)-datetime.fromisoformat(generated)).total_seconds() <= 26*3600
    result = dict(offer)
    if fresh and len(peers) >= 5:
        prices = sorted(peers)
        reference = median(prices)
        if (prices[len(prices)*3//4] - prices[len(prices)//4]) / reference <= .5:
            result["price_comparison"] = {"discount_pct": round((1-offer["price_per_m2"]/reference)*100, 1),
                "reference_price_per_m2": reference, "scope": "в том же ЖК", "comparable_count": len(peers)}
    return _public_offer(result)


def _deposit_changes(catalog: dict) -> list[dict]:
    programs = {offer.get("id"): offer for offer in catalog.get("offers", []) if isinstance(offer, dict)}
    sources = {source.get("url"): source for source in catalog.get("sources", []) if isinstance(source, dict)}

    def reviewed_after(record, event):
        if (record.get("status") != "verified" or record.get("pending_terms_hash")
                or record.get("pending_source_hash") or record.get("review_required")):
            return False
        try:
            checked = datetime.fromisoformat(record["checked_at"].replace("Z", "+00:00"))
            changed = datetime.fromisoformat(event["at"].replace("Z", "+00:00"))
            return checked.tzinfo is not None and changed.tzinfo is not None and checked >= changed
        except (ValueError, TypeError, KeyError, AttributeError):
            return False
    labels = {"conditions_changed": "Изменились условия: программа требует повторной проверки.",
              "document_changed": "Изменён официальный документ условий банка.",
              "programs_changed": "В каталоге банка изменился список программ."}
    changes = []
    for event in reversed(catalog.get("changes", [])[-12:]):
        if not isinstance(event, dict):
            continue
        offer = programs.get(event.get("id"), {})
        title = " · ".join(filter(None, [offer.get("bank"), offer.get("name")])) or "Условия банков"
        kind = event.get("kind")
        if kind == "rate_changed":
            before, after = event.get("old_effective_pct"), event.get("new_effective_pct")
            if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 30 for value in (before, after)):
                continue
            label = f"ГЭСВ: {before:g}% → {after:g}%".replace(".", ",")
        elif kind in labels:
            label = labels[kind]
            if kind == "conditions_changed" and reviewed_after(offer, event):
                label = "Изменение условий проверено после этого события."
            elif kind == "document_changed" and reviewed_after(sources.get(event.get("url"), {}), event):
                label = "Изменение документа проверено после этого события."
        else:
            continue
        try:
            date_label = datetime.fromisoformat(event["at"].replace("Z", "+00:00")).astimezone(LOCAL_TIMEZONE).strftime("%d.%m.%Y, %H:%M")
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        changes.append({"title": title, "label": label, "date_label": date_label})
    return changes


def comparison_context(*, hex_id: str | None = None, complex_id: int | None = None,
                       rooms: int = 2, area: float = 60, scope: str = "hex",
                       listing: str = "", unit: int | None = None, overrides: dict | None = None,
                       manual: bool = False) -> dict:
    market = load_market_snapshot()
    catalog = load_deposit_catalog()
    picks = load_daily_picks()
    details_by_id = {row["id"]: {**row, "current_price": row["price"]}
                     for row in market.get("offers", []) if row["market_type"] == "secondary"}
    details_by_id.update({row["id"]: row for row in market.get("listing_details", [])})
    secondary = []
    for item in (picks or {}).get("items", []):
        detail = details_by_id.get(item["id"])
        enriched = {**item, "market_type": "secondary"}
        if detail:
            enriched.update(details_for_selected_offer(detail, item["price"], item["area"], item["rooms"]))
            if detail.get("current_price", item["price"]) != item["price"] or source_characteristics_changed(detail, item):
                enriched["price_comparison"] = None
        # An older market edition may have no usable address. Preserve the
        # selected edition's street/house after sanitizing both sources.
        address, address_note = sanitized_listing_address(enriched.get("address"), item["price"], datetime.now(timezone.utc))
        if address is None:
            address, address_note = sanitized_listing_address(item.get("address"), item["price"], datetime.now(timezone.utc))
        enriched["address"], enriched["address_note"] = address, address_note
        secondary.append(_public_offer(enriched))
    selected = next((offer for offer in secondary if offer["id"] == listing), None) if listing else None
    primary = public_primary_offer(market, unit) if unit else None
    if primary:
        selected = _primary_selection_offer(primary, market)
    target = None
    if primary:
        target = primary
    elif selected:
        target = next((offer for offer in market.get("offers", []) if offer["id"] == selected["id"]), None)
    elif complex_id:
        target = next((offer for offer in market.get("offers", []) if offer["market_type"] == "primary" and offer["complex_id"] == complex_id), None)
    if target and not hex_id:
        hex_id = target["hex_id"]
        if selected:
            rooms, area = selected["rooms"], selected["area"]
    comparison = compare_market(market, hex_id=hex_id, rooms=rooms, area=area, scope=scope)
    primary_choices = [_primary_selection_offer(offer, market) for offer in comparison["apartments"]]
    choices = primary_choices + secondary
    by_id = {offer["id"]: offer for offer in market.get("offers", [])}
    radius = SCOPE_RADII[comparison["scope"]]
    local_secondary = [offer for offer in secondary if offer["id"] in by_id
                       and offer["rooms"] == comparison["rooms"]
                       and comparison["area_min"] <= offer["area"] <= comparison["area_max"]
                       and (radius is None or hex_distance(comparison["selected_hex"], by_id[offer["id"]]["hex_id"]) <= radius)]
    local_choices = primary_choices[:3] + local_secondary[:3] if local_secondary else primary_choices[:6]
    if selected and not any(offer["id"] == selected["id"] for offer in choices):
        choices.insert(0, selected)
    if not selected and not listing and not unit and not manual:
        selected = choices[0] if choices else None
    if selected:
        active = (not comparison.get("stale", True) if selected["market_type"] == "primary"
                  else bool(picks and not snapshot_status(picks)["is_stale"]))
        selected = {**selected, "price_forecast": forecast_for_offer(selected["id"], selected["price"], active=active)}
    form = {"price": selected["price"] if selected else 40_000_000, "repair_cost": 0,
            "buy_cost": 0, "monthly_rent": 0, "annual_owner_cost": 0, "vacancy_months": 0,
            "annual_growth_pct": 0, "wait_months": selected["wait_months"] if selected else 0,
            "sell_cost_pct": 0, "mode": "live", "listing": selected["id"] if selected else ""}
    form.update(overrides or {})
    errors = []
    if (listing or unit) and selected is None:
        errors.append("Выбранное объявление отсутствует в свежем публичном снимке. Выберите другой вариант или укажите цену вручную.")
    try:
        calculation = compare_investment(form, catalog=catalog)
    except ValueError as error:
        calculation = None
        errors.append(str(error))
    return {"comparison": comparison, "deposits": public_deposit_catalog(catalog),
            "calculation": calculation, "form_values": form, "offer_choices": choices,
            "local_offer_choices": local_choices, "calculation_error": " ".join(errors),
            "selected_offer": selected, "errors": errors,
            "deposit_changes": _deposit_changes(catalog),
            "manual_price_example": selected is None and overrides is None}


def register_public_comparison_routes(app, templates) -> None:
    def render(request: Request, context: dict, status_code: int = 200):
        return templates.TemplateResponse(request=request, name="market_comparison.html",
            context={"request": request, **context}, status_code=status_code,
            headers={"Cache-Control": "no-store"})

    @app.get("/compare", response_class=HTMLResponse)
    async def public_compare(request: Request, hex: str | None = Query(default=None, max_length=16),
                             complex: int | None = Query(default=None, ge=1, le=2_147_483_647),
                             rooms: int = Query(default=2, ge=1, le=6),
                             area: float = Query(default=60, ge=20, le=300),
                             scope: str = Query(default="hex", pattern="^(hex|ring1|ring2|ring4|ring8|city)$"),
                             listing: str = Query(default="", max_length=20, pattern="^(\\d{0,20}|newbuild:\\d{1,10})$"),
                             unit: int | None = Query(default=None, ge=1, le=2_147_483_647)):
        if hex is not None and not valid_hex(hex):
            return JSONResponse({"error": "Неверный участок карты"}, status_code=400)
        manual = "listing" in request.query_params and not listing and unit is None
        if listing.startswith("newbuild:"):
            unit, listing = int(listing.split(":", 1)[1]), ""
            if not 1 <= unit <= 2_147_483_647:
                return JSONResponse({"error": "Неверный номер квартиры"}, status_code=422)
        if complex is not None and not any(offer["market_type"] == "primary" and offer["complex_id"] == complex for offer in load_market_snapshot().get("offers", [])):
            return not_found_page(templates, request, "Для этого ЖК пока нет свежих данных сравнения", back_url="/compare", back_label="Выбрать другой ЖК")
        context = comparison_context(hex_id=hex, complex_id=complex, rooms=rooms,
                      area=area, scope=scope, listing=listing, unit=unit, manual=manual)
        return render(request, context, status_code=400 if context["errors"] else 200)

    @app.post("/compare/calculate", response_class=HTMLResponse)
    async def comparison_form(request: Request):
        if len(await request.body()) > 12_000:
            return JSONResponse({"error": "Слишком большой запрос"}, status_code=413)
        data = dict(await request.form(max_fields=24, max_files=0))
        identifier = str(data.get("listing", ""))
        listing = identifier if identifier.isdecimal() and len(identifier) <= 20 else ""
        unit = int(identifier.split(":", 1)[1]) if identifier.startswith("newbuild:") and identifier.split(":", 1)[1].isdecimal() and len(identifier) <= 20 else None
        if identifier and not listing and (unit is None or not 1 <= unit <= 2_147_483_647):
            return JSONResponse({"error": "Неверный номер квартиры"}, status_code=422)
        try:
            hex_id = data.get("hex") or None
            rooms, area = int(data.get("rooms", 2)), float(data.get("area", 60))
            scope = data.get("scope", "hex")
            if ((hex_id is not None and not valid_hex(hex_id)) or not 1 <= rooms <= 6
                    or not math.isfinite(area) or not 20 <= area <= 300 or scope not in SCOPE_RADII):
                raise ValueError("Неверные параметры области сравнения")
        except (ValueError, TypeError):
            return JSONResponse({"error": "Неверные параметры области сравнения"}, status_code=422)
        context = comparison_context(hex_id=hex_id, rooms=rooms, area=area, scope=scope,
                                     listing=listing, unit=unit, overrides=data, manual=not identifier)
        return render(request, context, status_code=400 if context["errors"] else 200)

    @app.post("/api/public/compare-investment")
    async def comparison_calculator(request: Request):
        body = await request.body()
        if len(body) > 12_000:
            return JSONResponse({"error": "Слишком большой запрос"}, status_code=413)
        try:
            payload = await request.json()
            result = compare_investment(payload, catalog=load_deposit_catalog())
        except (ValueError, TypeError) as error:
            return JSONResponse({"error": str(error)}, status_code=422, headers={"Cache-Control": "no-store"})
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
