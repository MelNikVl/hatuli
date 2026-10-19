"""The web card uses the same buyer analysis as Telegram, behind its access guard."""
from __future__ import annotations

from datetime import datetime, timezone
from datetime import timedelta
from functools import lru_cache
import json
import math
from pathlib import Path
import re

from bot.core.buyer import analyze_for_buyer
from bot.db import pg

SOURCE_CHECKS_PATH = Path(__file__).resolve().parents[2] / "var" / "browser-source-checks.json"
_PRICE_CLAIM = re.compile(r"\b(?:дешевле|дороже)\b|deal\s*score|(?:выгоднее|менее выгодна)\s+относительно\s+аналогов", re.IGNORECASE)


def source_price_warning(source, label):
    source_price = f"{source['price']:,.0f}".replace(",", " ")
    stored_price = f"{source['stored_price']:,.0f}".replace(",", " ")
    checked_label = datetime.fromisoformat(source["checked_at"]).strftime("%d.%m.%Y %H:%M UTC")
    prefix = "При последней проверке" if source["is_fresh"] else "Расхождение цены из прошлой проверки"
    return (f"{label}. {prefix} ({checked_label}): на Krisha {source_price} ₸, в Hatuli {stored_price} ₸. "
            "Текущую цену и доступность нужно проверить заново.")


@lru_cache(maxsize=2)
def _source_checks(path: str, modified: int, size: int):
    try:
        if size > 2_000_000:
            return []
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data["records"] if isinstance(data, dict) and data.get("schema_version") == 1 and isinstance(data.get("records"), list) else []
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return []


def observed_source(identifier, current_price, *, now=None, path=None):
    """Fresh confirmation expires; a known unresolved price mismatch does not.

    The newest valid observation wins by its timestamp, irrespective of file
    order. Historic availability is never reported as current availability.
    """
    source = Path(path or SOURCE_CHECKS_PATH)
    now = now or datetime.now(timezone.utc)
    try:
        stat = source.stat()
        records = _source_checks(str(source), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    latest = None
    for item in records:
        if not isinstance(item, dict) or item.get("listing_id") != str(identifier):
            continue
        try:
            checked = datetime.fromisoformat(item["checked_at"].replace("Z", "+00:00"))
            price = item["source_price"]
            if (checked.tzinfo is None or checked > now
                    or item.get("source_listing_id") != str(identifier)
                    or item.get("url") != f"https://krisha.kz/a/show/{identifier}"
                    or not isinstance(item.get("source_active"), bool)
                    or not isinstance(price, (int, float)) or isinstance(price, bool) or not math.isfinite(price) or price <= 0):
                continue
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if latest is None or checked >= latest[0]:
            latest = (checked, item, price)
    if latest is None:
        return None
    checked, item, price = latest
    fresh = now <= checked + timedelta(hours=2)
    matches = price == current_price
    if not fresh and matches:
        return None
    return {"checked_at": checked.astimezone(timezone.utc).isoformat(), "price": price,
            "active": item["source_active"] if fresh else None,
            "historical_active": item["source_active"], "is_fresh": fresh,
            "status": "fresh" if fresh else "historical_unresolved",
            "price_matches": matches, "stored_price": current_price}


def publication_summary(rows: list[dict], identity_status: str | None) -> dict:
    """Different source values are discrepancies, not proof of fraud or identity."""
    items = []
    for row in rows:
        observed = row.get("last_seen")
        items.append({"id": str(row["id"]), "url": row.get("url"), "rooms": row.get("rooms"),
                      "area": float(row["area"]) if row.get("area") else None,
                      "price": row.get("price"), "active": row.get("is_active") is True,
                      "observed_at": observed.isoformat() if observed else None})
    differences = []
    for field, label in (("rooms", "Комнаты"), ("area", "Площадь")):
        values = [item for item in items if item[field] is not None]
        distinct = {round(item[field], 1) if field == "area" else item[field] for item in values}
        if len(distinct) > 1:
            differences.append({"field": field, "label": label, "observations": [
                {"listing_id": item["id"], "url": item["url"], "value": item[field], "observed_at": item["observed_at"]}
                for item in values]})
    confirmed = identity_status == "confirmed"
    return {"count": len(items), "identity_confirmed": confirmed, "items": items, "discrepancies": differences,
            "message": ("Связанная история квартиры пока не найдена."
                        if not items else
                        "Пока известна только одна публикация. Повторные объявления и расхождения квартиры не подтверждены."
                        if len(items) == 1 else
                        "Связанные публикации найдены; совпадение физической квартиры пока требует проверки."
                        if not confirmed else "Публикации относятся к подтверждённой квартире.")}


async def build_buyer_journey(identifier: str) -> dict:
    result = await analyze_for_buyer(identifier, allow_import=False)
    if not result.get("found"):
        return {"found": False, "message": result.get("message")}
    listing = result["listing"]
    source_check = observed_source(identifier, listing.get("price"))
    base_price_mismatch = bool(source_check and source_check.get("price_matches") is False)
    links = await pg.fetch("""
        SELECT a.id,a.url,a.rooms,a.area,a.price,a.last_seen,a.is_active,p.identity_status
        FROM property_listings pl JOIN apartment_listings a ON a.id=pl.listing_id
        JOIN properties p ON p.property_id=pl.property_id
        WHERE pl.property_id=(SELECT property_id FROM property_listings WHERE listing_id=$1)
        ORDER BY a.last_seen DESC NULLS LAST,a.id LIMIT 30
    """, identifier)
    if not links:
        links = [await pg.fetchrow("SELECT id,url,rooms,area,price,last_seen,is_active FROM apartment_listings WHERE id=$1", identifier)]
    rows = [dict(row) for row in links if row]
    publications = publication_summary(rows, rows[0].get("identity_status") if rows else None)
    alternatives = []
    for candidate in result.get("better_nearby", [])[:3]:
        item = candidate["listing"]
        alternative_source = observed_source(str(item["id"]), item.get("price"))
        warnings = list(candidate.get("warnings", []))
        alternative_price_mismatch = bool(alternative_source and alternative_source.get("price_matches") is False)
        price_mismatch = base_price_mismatch or alternative_price_mismatch
        advantages = list(candidate.get("advantages", []))
        tradeoffs = list(candidate.get("tradeoffs", []))
        if price_mismatch:
            advantages = [value for value in advantages if not _PRICE_CLAIM.search(str(value))]
            tradeoffs = [value for value in tradeoffs if not _PRICE_CLAIM.search(str(value))]
            price_warnings = ["Ценовые преимущества и рейтинг не подтверждены: обнаружено расхождение цены. "
                              "Проверьте актуальные цены обоих объявлений."]
            if base_price_mismatch:
                price_warnings.append(source_price_warning(source_check, "Выбранная квартира"))
            if alternative_price_mismatch:
                price_warnings.append(source_price_warning(alternative_source, "Альтернатива"))
            warnings = price_warnings + warnings
        alternatives.append({"id": str(item["id"]), "url": item.get("url"),
            "rooms": item.get("rooms"), "area": item.get("area"), "price": item.get("price"),
            "distance_m": candidate.get("distance_m"), "advantages": advantages,
            "tradeoffs": tradeoffs, "warnings": warnings,
            "category": "similar" if price_mismatch else candidate.get("category"), "active_in_database": item.get("is_active") is True,
            "source_check": alternative_source})
    no_alternatives = ("Без координаты нельзя надёжно выбрать квартиры рядом."
                       if listing.get("lat") is None or listing.get("lon") is None else
                       "В радиусе 2 км не нашли подходящих квартир с тем же числом комнат и близкой площадью и ценой.")
    alternatives_message = (no_alternatives if not alternatives else
        f"Найдено {len(alternatives)} из максимум трёх вариантов. Сравнение по наблюдаемым предложениям, доступность уточняйте в источнике.")
    if base_price_mismatch:
        alternatives_message += " Подбор выполнен по сохранённой цене; после уточнения цены набор альтернатив нужно обновить."
    return {"found": True, "as_of": datetime.now(timezone.utc).isoformat(),
            "verdict": result.get("verdict"), "confidence": result.get("confidence"),
            "warnings": result.get("warnings", []), "publications": publications,
            "alternatives": alternatives, "alternatives_message": alternatives_message,
            "source_url": listing.get("url"), "source_check": source_check}
