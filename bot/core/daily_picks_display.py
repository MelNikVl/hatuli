"""Russian presentation labels for the validated daily public snapshot."""
from __future__ import annotations

from datetime import date, datetime, timezone

from bot.core.daily_picks import LOCAL_TIMEZONE, MAX_ASKING_PRICE, sanitized_listing_address, snapshot_status
from bot.core.price_forecast_runtime import forecast_for_offer
from bot.core.market_comparison import load_market_snapshot
from bot.core.comparison_offer_details import details_for_selected_offer, source_characteristics_changed
from bot.core.apartment_selection_reasons import best_apartment_reasons
from bot.core.daily_pick_identity import validate_public_daily_identity
from bot.core.comparison_rental_estimate import validate_rental_estimate

_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(LOCAL_TIMEZONE)


def _day_label(value: date) -> str:
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


def _time_label(value: str) -> str:
    timestamp = _timestamp(value)
    return f"{timestamp.day} {_MONTHS[timestamp.month - 1]}, {timestamp:%H:%M}"


def prepare_daily_picks(snapshot: dict | None, now: datetime | None = None) -> dict | None:
    if snapshot is None:
        return None
    now = now or datetime.now(timezone.utc)
    status = snapshot_status(snapshot, now=now)
    selection_date = date.fromisoformat(snapshot["selection_date"])
    is_today = selection_date == now.astimezone(LOCAL_TIMEZONE).date()
    if status["is_stale"]:
        message = "Новый выпуск пока не опубликован. Перед просмотром уточните цену и доступность у продавца."
    elif not is_today:
        message = "Следующий выпуск — сегодня в 08:00 по времени Астаны. Пока доступна предыдущая подборка."
    else:
        message = "Новый выпуск — каждый день после 08:00 по времени Астаны. В каждой карточке указано время проверки объявления."
    market = load_market_snapshot()
    details = {row["id"]: {**row, "current_price": row["price"]}
               for row in market.get("offers", []) if row["market_type"] == "secondary"}
    details.update({row["id"]: row for row in market.get("listing_details", [])})
    items = []
    for item in snapshot["items"]:
        try:
            identity = validate_public_daily_identity(item.get("complex_identity"))
        except (ValueError, TypeError):
            continue
        latest_price = details.get(item["id"], {}).get("current_price", item["price"])
        if item["price"] > MAX_ASKING_PRICE or latest_price > MAX_ASKING_PRICE:
            continue
        enriched = {**item}
        enriched["complex_identity"] = identity
        enriched["complex_name"] = identity["canonical_name"] or ""
        if item["id"] in details:
            enriched.update(details_for_selected_offer(details[item["id"]], item["price"], item["area"], item["rooms"]))
            if details[item["id"]].get("current_price", item["price"]) != item["price"]:
                continue
            if source_characteristics_changed(details[item["id"]], item):
                continue
            # The daily edition owns its independently checked facts. An older
            # market file cannot remove or promote owner/house confirmation.
            enriched["selection_facts"] = item.get("selection_facts")
            enriched["complex_identity"] = identity
            enriched["complex_name"] = identity["canonical_name"] or ""
            own_history = item.get("price_history") or {}
            merged_history = enriched.get("price_history") or {}
            own_last = max((event["at"] for event in own_history.get("events", [])), default="")
            merged_last = max((event["at"] for event in merged_history.get("events", [])), default="")
            if own_last >= merged_last:
                enriched["price_history"] = item.get("price_history")
        # An older market edition may lack an address that the selected public
        # edition already has. Neither the catalog name nor a guessed map point
        # can supply a missing street or house number.
        address, address_note = sanitized_listing_address(enriched.get("address"), item["price"], now)
        if address is None:
            address, address_note = sanitized_listing_address(item.get("address"), item["price"], now)
        enriched["address"], enriched["address_note"] = address, address_note
        enriched["rental_estimate"] = validate_rental_estimate(enriched.get("rental_estimate"), now)
        enriched["reasons"] = best_apartment_reasons(enriched, now)
        if len(enriched["reasons"]) != 4:
            continue
        # Actual history gaps remain beside their chart; do not repeat them
        # as a generic pre-viewing checklist below every apartment.
        enriched["evidence_notices"] = (
            ["В залоге — указано в исходном объявлении."]
            if (item.get("source_facts") or {}).get("is_pledged") is True else []
        )
        enriched.update(observed_at_label=_time_label(item["observed_at"]),
                        price_forecast=forecast_for_offer(item["id"], item["price"], now=now, active=not status["is_stale"]),
                        source_checked_at_label=_time_label(item["source_checked_at"]))
        enriched["rank"] = len(items) + 1
        items.append(enriched)
    return {
        **snapshot,
        "date_label": _day_label(selection_date),
        "generated_at_label": _time_label(snapshot["generated_at"]),
        "as_of_label": _time_label(snapshot["source_observed_at"]),
        "is_stale": status["is_stale"], "is_today": is_today,
        "status_message": message,
        "items": items,
    }
