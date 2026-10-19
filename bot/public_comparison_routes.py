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
from bot.core.apartment_selection_reasons import (
    apartment_selection_reasons, best_apartment_reasons, validate_selection_facts,
)
from bot.core.daily_pick_identity import validate_public_daily_identity
from bot.core.comparison_rental_estimate import validate_rental_estimate
from bot.core.automatic_investment import calculate_automatic_investment, select_complex
from bot.core.comparison_complexes import load_comparison_complexes


# Labels and constraints are presentation only: the pure calculation validates
# every enabled strategy again. Zeroes are visible, editable user assumptions.
STRATEGY_FIELDS = {
    "monthly": [
        ("purchase_price", "Цена покупки, ₸", 1, 1000000000, 1, "", False),
        ("monthly_rent", "Аренда за месяц, ₸", 0, 10000000, 1, "", False),
        ("exit_value", "Стоимость объекта через год, ₸", 0, 1000000000, 1, "", False),
        ("vacancy_months", "Простой после готовности, месяцев", 0, 12, .1, "", False),
        ("repair_cost", "Ремонт и обустройство, ₸", 0, 500000000, 1, 0, True),
        ("buy_cost", "Оформление и покупка, ₸", 0, 100000000, 1, 0, True),
        ("annual_owner_cost", "Содержание, налоги и обслуживание за год, ₸", 0, 100000000, 1, "", True),
        ("wait_months", "До готовности к сдаче, месяцев", 0, 60, .1, 0, True),
        ("sell_cost_pct", "Расходы выхода из объекта, %", 0, 30, .1, 0, True),
    ],
    "daily": [
        ("purchase_price", "Цена покупки, ₸", 1, 1000000000, 1, "", False),
        ("adr", "Средняя ставка оплаченной ночи, ₸", 0, 1000000, 1, "", False),
        ("occupancy_pct", "Доля оплаченных ночей, %", 0, 100, .1, "", False),
        ("exit_value", "Стоимость объекта через год, ₸", 0, 1000000000, 1, "", False),
        ("platform_fee_pct", "Комиссия площадки, % выручки", 0, 100, .1, "", True),
        ("management_fee_pct", "Управление, % выручки", 0, 100, .1, "", True),
        ("cleaning_cost_per_stay", "Уборка за одно заселение, ₸", 0, 1000000, 1, "", True),
        ("stays_per_month", "Заселений за рабочий месяц", 0, 31, .1, "", True),
        ("repair_cost", "Ремонт и оснащение, ₸", 0, 500000000, 1, 0, True),
        ("buy_cost", "Оформление и покупка, ₸", 0, 100000000, 1, 0, True),
        ("annual_owner_cost", "Коммунальные, налоги, обслуживание за год, ₸", 0, 100000000, 1, "", True),
        ("wait_months", "До готовности к сдаче, месяцев", 0, 60, .1, 0, True),
        ("sell_cost_pct", "Расходы выхода из объекта, %", 0, 30, .1, 0, True),
    ],
    "construction": [
        ("entry_price", "Цена входа в проект, ₸", 1, 1000000000, 1, "", False),
        ("wait_months", "До готовности проекта, месяцев", 0, 60, .1, "", False),
        ("exit_value", "Стоимость объекта или права через год, ₸", 0, 1000000000, 1, "", False),
        ("finishing_cost", "Отделка и обустройство, ₸", 0, 500000000, 1, "", True),
        ("buy_cost", "Оформление и покупка, ₸", 0, 100000000, 1, 0, True),
        ("annual_owner_cost", "Прочие расходы за год, ₸", 0, 100000000, 1, "", True),
        ("sell_cost_pct", "Продажа или переуступка, %", 0, 30, .1, "", True),
    ],
}
STRATEGY_LABELS = {"monthly": "Месячная аренда", "daily": "Посуточная аренда", "construction": "Вложения в стройку"}


def _strategy_form(selected_offer=None, payload=None):
    """Only an amount and an optional catalogue ID remain public inputs."""
    payload = payload if isinstance(payload, dict) else {}
    selected_offer = selected_offer or {}
    selected_price = selected_offer.get("price", "")
    values = {"budget": payload.get("budget", selected_price), "complex_id": payload.get("complex_id", "")}
    if not isinstance(values["budget"], (str, int, float)) or isinstance(values["budget"], bool):
        values["budget"] = ""
    else:
        values["budget"] = str(values["budget"])[:100]
    value = values["complex_id"]
    values["complex_id"] = str(value)[:20] if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    return values


def _strategy_chart(result):
    """Plot operating cash only. Never interpolate asset values or bank GESV."""
    series = []
    for index, row in enumerate(result.get("strategies", [])):
        timeline = row.get("timeline")
        if not isinstance(timeline, list) or len(timeline) != 12:
            continue
        points = [0]
        for month, item in enumerate(timeline, 1):
            value = item.get("cumulative_net_cash") if isinstance(item, dict) and item.get("month") == month else None
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                break
            points.append(value)
        if len(points) == 13:
            series.append({"key": row["key"], "label": row["label"], "values": points, "color": index % 3})
    if not series:
        return None
    values = [value for item in series for value in item["values"]]
    low, high = min(values), max(values)
    span = high - low or max(abs(high), 1)
    low, high = low - span * .08, high + span * .08
    for item in series:
        item["points"] = " ".join(f"{50+month*48:.2f},{208-(value-low)/(high-low)*178:.2f}" for month, value in enumerate(item["values"]))
    return {"series": series, "low": low, "high": high,
            "zero_y": 208-(0-low)/(high-low)*178}


def _public_offer(offer: dict, *, daily_identity: dict | None = None,
                  daily_mismatch_notices: tuple | list = ()) -> dict:
    primary = offer.get("market_type") == "primary"
    wait = 0 if not primary or offer.get("build_status") == "commissioned" else 12
    identifier = offer["id"]
    params = {"unit": identifier.removeprefix("newbuild:")} if primary else {"listing": identifier}
    title = f"{offer['rooms']}-комнатная · {offer['area']:g} м²".replace(".", ",")
    cautions = [] if daily_identity is not None else list(offer.get("cautions", []))
    if primary and wait:
        cautions.append("Для неподтверждённой готовности выбран сценарий ожидания 12 месяцев. Уточните срок ключей и измените его в расчёте.")
    observed = offer.get("source_checked_at") or offer.get("observed_at")
    observed_label = datetime.fromisoformat(observed.replace("Z", "+00:00")).astimezone(LOCAL_TIMEZONE).strftime("%d.%m.%Y, %H:%M") if observed else None
    reasons = (best_apartment_reasons(offer) if daily_identity is not None and not daily_mismatch_notices
               else apartment_selection_reasons(offer))
    result = {
        "id": identifier, "market_type": "primary" if primary else "secondary", "title": title,
        "complex_name": offer.get("complex_name"), "rooms": offer["rooms"], "area": offer["area"],
        "price": offer["price"], "url": f"/listing/nb-{identifier.removeprefix('newbuild:')}" if primary else offer.get("url") or offer.get("source_url"),
        "href": "/compare?" + urlencode(params) + "#investment", "wait_months": wait,
        "finish_label": offer.get("finish_label") if offer.get("finish_level") else None,
        "observed_at": offer.get("observed_at"), "source_label": offer.get("source_label", "Krisha.kz"),
        "observed_at_label": observed_label,
        "build_status_label": offer.get("build_status_label") if offer.get("build_status") in {"under_construction", "commissioned"} else None,
        "cautions": cautions,
        "reasons": reasons,
        **{field: offer.get(field) for field in DETAIL_FIELDS},
        "rental_estimate": validate_rental_estimate(offer.get("rental_estimate")),
    }
    if daily_identity is not None:
        source_facts = offer.get("source_facts") or {}
        pledged = isinstance(source_facts, dict) and source_facts.get("is_pledged") is True
        notices = ([f"В залоге — указано на Krisha.kz при проверке {observed_label}."
                    if observed_label else "В залоге — указано в исходном объявлении Krisha.kz."]
                   if pledged else [])
        result.update(
            from_daily_picks=True, is_daily_pick=not daily_mismatch_notices and len(reasons) == 4,
            complex_identity=daily_identity, complex_name=daily_identity["canonical_name"] or "",
            floor=offer.get("floor") if not daily_mismatch_notices else None,
            floors_total=offer.get("floors_total") if not daily_mismatch_notices else None,
            selection_facts=validate_selection_facts(offer.get("selection_facts"), offer["area"]),
            source_facts={"is_pledged": True} if pledged else {},
            source_checked_at=offer.get("source_checked_at"), source_status=offer.get("source_status"),
            evidence_notices=[*notices, *daily_mismatch_notices],
        )
    return result


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
        try:
            daily_identity = validate_public_daily_identity(item.get("complex_identity"))
        except (ValueError, TypeError):
            daily_identity = None
        detail = details_by_id.get(item["id"])
        enriched = {**item, "market_type": "secondary"}
        mismatch_notices = []
        if detail:
            enriched.update(details_for_selected_offer(detail, item["price"], item["area"], item["rooms"]))
            price_changed = detail.get("current_price", item["price"]) != item["price"]
            characteristics_changed = source_characteristics_changed(detail, item)
            if price_changed or characteristics_changed:
                enriched["price_comparison"] = None
            if daily_identity is not None:
                if price_changed:
                    mismatch_notices.append("Цена проверенной карточки отличается от цены последнего обновления базы.")
                if characteristics_changed:
                    mismatch_notices.append("Характеристики объявления изменились после проверки карточки.")
                if not mismatch_notices:
                    # The selected edition owns its browser-checked benefits.
                    # A market cache may add later price events, never replace
                    # the checked owner, measurements or canonical identity.
                    enriched["selection_facts"] = item.get("selection_facts")
                    own_history = item.get("price_history") or {}
                    merged_history = enriched.get("price_history") or {}
                    own_last = max((event["at"] for event in own_history.get("events", [])), default="")
                    merged_last = max((event["at"] for event in merged_history.get("events", [])), default="")
                    if own_last >= merged_last:
                        enriched["price_history"] = item.get("price_history")
        # An older market edition may have no usable address. Preserve the
        # selected edition's street/house after sanitizing both sources.
        address, address_note = sanitized_listing_address(enriched.get("address"), item["price"], datetime.now(timezone.utc))
        if address is None:
            address, address_note = sanitized_listing_address(item.get("address"), item["price"], datetime.now(timezone.utc))
        enriched["address"], enriched["address_note"] = address, address_note
        secondary.append(_public_offer(enriched, daily_identity=daily_identity,
                                       daily_mismatch_notices=mismatch_notices))
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
    if not hex_id:
        if target:
            hex_id = target["hex_id"]
        if selected:
            # A verified daily card can have supplemental details without a
            # usable market point. Its validated size still defines the peer
            # filter; choose a center from the market rather than inventing one.
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
    form = {"price": selected["price"] if selected else "", "repair_cost": 0,
            "buy_cost": 0, "monthly_rent": 0, "annual_owner_cost": 0, "vacancy_months": 0,
            "annual_growth_pct": 0, "wait_months": selected["wait_months"] if selected else 0,
            "sell_cost_pct": 0, "mode": "live", "listing": selected["id"] if selected else ""}
    form.update(overrides or {})
    errors = []
    if (listing or unit) and selected is None:
        errors.append("Выбранное объявление отсутствует в свежем публичном снимке. Выберите другое предложение.")
    calculation = None
    if selected or overrides is not None:
        try:
            calculation = compare_investment(form, catalog=catalog)
        except ValueError as error:
            errors.append(str(error))
    return {"comparison": comparison, "deposits": public_deposit_catalog(catalog),
            "calculation": calculation, "form_values": form, "offer_choices": choices,
            "local_offer_choices": local_choices, "calculation_error": " ".join(errors),
            "selected_offer": selected, "errors": errors,
            "deposit_changes": _deposit_changes(catalog),
            "manual_price_example": False}


def register_public_comparison_routes(app, templates) -> None:
    def render(request: Request, context: dict, status_code: int = 200):
        context.setdefault("complex_catalog", load_comparison_complexes())
        explicit_offer = bool(request.query_params.get("listing") or request.query_params.get("unit"))
        context.setdefault("strategy_form", _strategy_form(context.get("selected_offer") if explicit_offer else None))
        context.setdefault("strategy_result", None)
        context.setdefault("strategy_chart", None)
        context.setdefault("strategy_error", "")
        if context["strategy_result"] is None and context["strategy_form"].get("budget") and not context["strategy_error"]:
            try:
                context["strategy_result"] = calculate_automatic_investment(
                    context["strategy_form"], catalog=load_deposit_catalog(), market=load_market_snapshot(),
                    picks=load_daily_picks(), complexes=context["complex_catalog"])
            except (ValueError, TypeError, OverflowError) as exc:
                context["strategy_error"] = str(exc)
        return templates.TemplateResponse(request=request, name="market_comparison.html",
            context={"request": request, "strategy_fields": STRATEGY_FIELDS,
                     "strategy_labels": STRATEGY_LABELS, **context}, status_code=status_code,
            headers={"Cache-Control": "no-store"})

    @app.get("/compare", response_class=HTMLResponse)
    async def public_compare(request: Request, hex: str | None = Query(default=None, max_length=16),
                             complex: int | None = Query(default=None, ge=1, le=2_147_483_647),
                             rooms: int = Query(default=2, ge=1, le=6),
                             area: float = Query(default=60, ge=20, le=300),
                             scope: str = Query(default="hex", pattern="^(hex|ring1|ring2|ring4|ring8|city)$"),
                             listing: str = Query(default="", max_length=20, pattern="^(\\d{0,20}|newbuild:\\d{1,10})$"),
                             unit: int | None = Query(default=None, ge=1, le=2_147_483_647),
                             budget: str | None = Query(default=None, max_length=80),
                             complex_id: int | None = Query(default=None, ge=1, le=2_147_483_647)):
        if hex is not None and not valid_hex(hex):
            return JSONResponse({"error": "Неверный участок карты"}, status_code=400)
        manual = not listing and unit is None
        if listing.startswith("newbuild:"):
            unit, listing = int(listing.split(":", 1)[1]), ""
            if not 1 <= unit <= 2_147_483_647:
                return JSONResponse({"error": "Неверный номер квартиры"}, status_code=422)
        complexes = load_comparison_complexes()
        auto_complex_id = complex_id if complex_id is not None else complex
        try:
            select_complex(auto_complex_id, complexes, datetime.now(timezone.utc))
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=422, headers={"Cache-Control": "no-store"})
        context = comparison_context(hex_id=hex, complex_id=complex, rooms=rooms,
                      area=area, scope=scope, listing=listing, unit=unit, manual=manual)
        selected = context.get("selected_offer") if (listing or unit) else None
        values = {"complex_id": auto_complex_id or ""}
        if budget is not None:
            values["budget"] = budget
        context.update(complex_catalog=complexes, strategy_form=_strategy_form(selected, values))
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
        context["strategy_form"] = _strategy_form(context.get("selected_offer") if identifier else None)
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

    @app.post("/compare/strategies/calculate")
    async def strategy_comparison_calculator(request: Request):
        headers = {"Cache-Control": "no-store"}
        if len(await request.body()) > 16_000:
            return JSONResponse({"error": "Слишком большой запрос"}, status_code=413, headers=headers)
        is_json = "application/json" in request.headers.get("content-type", "")
        wants_json = is_json or "application/json" in request.headers.get("accept", "")
        payload, data = {}, {}
        try:
            if is_json:
                payload = await request.json()
                if not isinstance(payload, dict):
                    raise ValueError("Требуется объект параметров")
                data = {}
            else:
                data = dict(await request.form(max_fields=80, max_files=0))
                payload = {"budget": data.get("budget", ""), "complex_id": data.get("complex_id", "")}
            result = calculate_automatic_investment(
                payload, catalog=load_deposit_catalog(), market=load_market_snapshot(),
                picks=load_daily_picks(), complexes=load_comparison_complexes())
            error = ""
        except (ValueError, TypeError, OverflowError) as exc:
            result, error = None, str(exc)
            if error.startswith("budget:"):
                error = "Общий бюджет:" + error[len("budget:"):]
            elif error.startswith("horizon_months:"):
                error = "Срок сравнения:" + error[len("horizon_months:"):]
            if wants_json:
                return JSONResponse({"error": error}, status_code=422, headers=headers)
        chart = None
        if wants_json:
            html = templates.env.get_template("_investment_strategy_results.html").render(
                strategy_result=result, strategy_chart=chart)
            return JSONResponse({"result": result, "html": html, "results_html": html}, headers=headers)
        # Rebuild the existing selected-offer context through the same public
        # snapshot gate as GET. A calculator submission cannot forge a receipt.
        identifier = str(data.get("listing", ""))
        listing = identifier if identifier.isdecimal() and len(identifier) <= 20 else ""
        unit = int(identifier.split(":", 1)[1]) if identifier.startswith("newbuild:") and identifier.split(":", 1)[1].isdecimal() and len(identifier) <= 20 else None
        if identifier and not listing and (unit is None or not 1 <= unit <= 2_147_483_647):
            return JSONResponse({"error": "Неверный номер квартиры"}, status_code=422, headers=headers)
        context = comparison_context(listing=listing, unit=unit, manual=not identifier)
        context.update(strategy_form=_strategy_form(context.get("selected_offer"), payload),
                       strategy_result=result, strategy_chart=chart, strategy_error=error)
        return render(request, context, status_code=400 if error else 200)
