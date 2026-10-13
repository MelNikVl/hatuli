"""Anonymous comparison pages read published files, never live/private databases."""
from __future__ import annotations

import math
from datetime import datetime
from urllib.parse import urlencode

from fastapi import Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from bot.core.daily_picks import load_daily_picks
from bot.core.deposit_comparison import compare_investment, load_deposit_catalog, public_deposit_catalog
from bot.core.market_comparison import (
    LOCAL_TIMEZONE, SCOPE_RADII, compare_market, hex_distance,
    load_market_snapshot, public_primary_offer, valid_hex,
)
from bot.core.web_pages import not_found_page


def _public_offer(offer: dict) -> dict:
    primary = offer.get("market_type") == "primary"
    wait = 0 if not primary or offer.get("build_status") == "commissioned" else 12
    identifier = offer["id"]
    params = {"unit": identifier.removeprefix("newbuild:")} if primary else {"listing": identifier}
    title = f"{offer['rooms']}-комнатная · {offer['area']:g} м²"
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
    }


def _deposit_changes(catalog: dict) -> list[dict]:
    programs = {offer.get("id"): offer for offer in catalog.get("offers", []) if isinstance(offer, dict)}
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
    secondary = [_public_offer({**item, "market_type": "secondary"}) for item in (picks or {}).get("items", [])]
    selected = next((offer for offer in secondary if offer["id"] == listing), None) if listing else None
    primary = public_primary_offer(market, unit) if unit else None
    if primary:
        selected = _public_offer(primary)
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
    primary_choices = [_public_offer(offer) for offer in comparison["apartments"]]
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
