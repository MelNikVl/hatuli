"""Russian presentation labels for the validated daily public snapshot."""
from __future__ import annotations

from datetime import date, datetime, timezone

from bot.core.daily_picks import LOCAL_TIMEZONE, snapshot_status
from bot.core.price_forecast_runtime import forecast_for_offer
from bot.core.market_comparison import load_market_snapshot
from bot.core.comparison_offer_details import details_for_selected_offer, source_characteristics_changed
from bot.core.apartment_selection_reasons import apartment_selection_reasons
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
        enriched = {**item}
        if item["id"] in details:
            enriched.update(details_for_selected_offer(details[item["id"]], item["price"], item["area"], item["rooms"]))
            if details[item["id"]].get("current_price", item["price"]) != item["price"]:
                enriched["price_comparison"] = None
                enriched["cautions"] = [*item.get("cautions", []), "Цена в подборке отличается от последнего обновления базы; проверьте её в источнике."]
            if source_characteristics_changed(details[item["id"]], item):
                enriched["price_comparison"] = None
                enriched["cautions"] = [*enriched.get("cautions", []), "Характеристики объявления изменились после проверки; уточните квартиру в источнике."]
        enriched["rental_estimate"] = validate_rental_estimate(enriched.get("rental_estimate"), now)
        enriched["reasons"] = apartment_selection_reasons(enriched, now)
        enriched.update(observed_at_label=_time_label(item["observed_at"]),
                        price_forecast=forecast_for_offer(item["id"], item["price"], now=now, active=not status["is_stale"]),
                        source_checked_at_label=_time_label(item["source_checked_at"]))
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
