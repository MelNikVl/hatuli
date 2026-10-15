"""Daily, explicit public projection of sale listings; no request-time DB access.

This ranking deliberately recomputes asking-price comparisons. Stored deal
scores can predate a price change and are neither transaction prices nor a
legal assessment. All input I/O belongs to the offline publisher.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from bot.core.apartment_selection_reasons import (
    apartment_selection_reasons, selection_facts, strong_selection_reason,
    validate_selection_facts, best_apartment_reasons,
)
from bot.core.daily_pick_identity import (
    normalize_daily_pick_identity, public_daily_pick_identity, validate_public_daily_identity,
)
from bot.core.comparison_offer_details import build_offer_details, price_history

LOCAL_TIMEZONE = ZoneInfo("Asia/Almaty")
SCHEMA_VERSION = 2
MAX_PICKS = 30
MIN_PICKS = 10
REQUIRED_FACTORS = 4
MAX_ASKING_PRICE = 100_000_000
MAX_SOURCE_AGE = timedelta(hours=72)
MIN_COMPARABLES = 5
AREA_BAND = .15
MAX_DISCOUNT = .30
MAX_PREMIUM = .05
NEARBY_RADIUS_M = 800
PHOTO_HOST = "krisha-photos.kcdn.online"
_ID = re.compile(r"[0-9]{5,20}\Z")
_PHOTO_PATH = re.compile(r"/(?:webp/)?[0-9a-f]{2}/[0-9a-f-]{36}/[0-9]+-(?:full|thumb)\.(?:jpg|jpeg|webp)\Z", re.I)
_DISTRICTS = {
    "Есильский р-н": "Есильский район", "Нура р-н": "Нура", "Алматы р-н": "Алматы",
    "Сарайшык р-н": "Сарайшык", "Сарыарка р-н": "Сарыарка", "р-н Байконур": "Байконур",
}
_ITEM_FIELDS = ("id", "rank", "source_url", "title", "rooms", "area", "floor", "floors_total", "complex_name", "complex_identity", "district", "address", "address_note", "price", "currency", "price_per_m2", "photo_urls", "reasons", "cautions", "observed_at", "source_checked_at", "source_status", "source_facts", "price_comparison", "selection_facts", "price_history")
_COMPARISON_FIELDS = ("discount_pct", "reference_price_per_m2", "scope", "comparable_count")
_COMPARISON_SCOPES = {"в том же доме", "в том же ЖК", "в радиусе 800 м по приблизительным координатам"}
_NEARBY_SCOPE = "в радиусе 800 м по приблизительным координатам"
_NEARBY_CAUTION = "Аналоги в радиусе 800 м; координаты приблизительные, расположение нужно уточнить."
_SNAPSHOT_FIELDS = ("schema_version", "selection_date", "timezone", "generated_at", "source_observed_at", "total_candidates", "items", "methodology", "publication_policy")
METHODOLOGY = {
    "title": "Свежая цена и понятные основания",
    "summary": "Отбираем только квартиры с ценой не менее чем на 3% ниже похожих предложений. У каждой ровно четыре независимых подтверждённых преимущества: цена обязательна, остальные — существенное или последовательное снижение цены, высокие потолки, большая кухня, проверенная пометка хозяина и этаж между первым и последним. Порог 3% — правило отбора, а не гарантия выгодной сделки. Процент скидки и разница в тенге — один фактор. Качество ЖК, отзывы, локация и ремонт по фото станут основаниями только после отдельной проверки.",
    "exclusions": ["Только вторичная продажа до 100 млн ₸ включительно; наличие каждого выбранного объявления и его цену проверяем на странице источника перед публикацией.", "До отбора и расчёта аналогов исключаем спорные привязки и дубли; у зонтика должен быть определён конкретный дом по адресу или обозначению корпуса. Точный адрес не придумываем.", "Новый выпуск содержит от 10 до 30 вариантов с подтверждёнными фотографиями интерьера и четырьмя преимуществами. Снижение цены должно иметь непротиворечивую историю без повышений в выбранном периоде: не менее 1 млн ₸ или 3%, либо не менее двух дат снижения и 1% суммарно. Цена на уровне аналогов или выше исключает квартиру независимо от остальных плюсов."],
    "caveat": "Это цены объявлений, а не состоявшихся сделок. Сравнение использует недавние наблюдения аналогов; не каждый аналог проверяется повторно на источнике. Ремонт и условия продажи могут отличаться. После публикации объявление может измениться: наличие, итоговую цену, документы и долги уточните у продавца до задатка.",
}


def snapshot_path() -> Path:
    return Path(os.environ.get("HATULI_DAILY_PICKS_PATH") or Path(__file__).resolve().parents[2] / "var" / "daily-picks.json")


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Timezone-aware observation required")
    return value.astimezone(timezone.utc)


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def safe_photo_url(value) -> str | None:
    if not isinstance(value, str) or len(value) > 400:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname != PHOTO_HOST or parsed.username or parsed.password
                or parsed.port is not None or parsed.query or parsed.fragment or not _PHOTO_PATH.fullmatch(parsed.path)):
            return None
    except ValueError:
        return None
    return value


def _valid_source(row: dict) -> bool:
    value = row.get("url")
    if not isinstance(value, str) or len(value) > 500:
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and parsed.hostname in {"krisha.kz", "www.krisha.kz"}
                and not parsed.username and not parsed.password and parsed.port is None
                and parsed.path.rstrip("/") == f"/a/show/{row['id']}")
    except (KeyError, ValueError):
        return False


def _list(value) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _identity(row: dict) -> tuple:
    return ("property", row["property_id"]) if row.get("property_id") is not None else ("listing", row["id"])


def _group(row: dict) -> tuple | None:
    # Geographic attribution is not a verified physical building.
    if row.get("resolved_house_id") and row.get("house_attribution") in {"address", "token"}:
        return ("house", row["resolved_house_id"], row["rooms"])
    if row.get("complex_id") and row.get("complex_resolution") in {"name", "url", "house"}:
        return ("complex", row["complex_id"], row["rooms"])
    return None


def _nearby(a: dict, b: dict) -> bool:
    lat_a, lon_a, lat_b, lon_b = (_number(value) for value in (a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")))
    if None in (lat_a, lon_a, lat_b, lon_b):
        return False
    if not all(50.85 <= lat <= 51.4 and 71.1 <= lon <= 71.85 for lat, lon in ((lat_a, lon_a), (lat_b, lon_b))):
        return False
    # Haversine, straight-line distance; never advertised as walking distance.
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    delta_phi, delta_lon = phi_b - phi_a, math.radians(lon_b - lon_a)
    h = math.sin(delta_phi / 2) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lon / 2) ** 2
    return 6_371_000 * 2 * math.asin(min(1., math.sqrt(h))) <= NEARBY_RADIUS_M


def _nearby_cell(row: dict) -> tuple[int, int] | None:
    lat, lon = _number(row.get("lat")), _number(row.get("lon"))
    if lat is None or lon is None or not 50.85 <= lat <= 51.4 or not 71.1 <= lon <= 71.85:
        return None
    # In these city bounds each cell is wider than 800 m on both axes.
    # Its eight neighbours therefore contain every possible nearby peer;
    # _nearby still applies the exact haversine distance afterwards.
    return math.floor(lat / .01), math.floor(lon / .015)


def _similar(a: dict, b: dict) -> bool:
    if _identity(a) == _identity(b) or a["rooms"] != b["rooms"] or not float(a["area"]) * (1 - AREA_BAND) <= float(b["area"]) <= float(a["area"]) * (1 + AREA_BAND):
        return False
    finish_a, finish_b = a.get("finish_level"), b.get("finish_level")
    return not finish_a or not finish_b or finish_a == finish_b


def _eligible(row: dict, now: datetime) -> bool:
    if not _ID.fullmatch(str(row.get("id", ""))) or not _valid_source(row) or row.get("is_active") is not True or row.get("is_duplicate") is True or row.get("dup_needs_review") is True or row.get("market_type") != "secondary":
        return False
    price, area, rooms = (_number(row.get(key)) for key in ("price", "area", "rooms"))
    if price is None or area is None or rooms is None or not (5_000_000 <= price <= 600_000_000 and 20 <= area <= 300 and rooms.is_integer() and 1 <= rooms <= 6 and 14 <= area / rooms <= 100 and 150_000 <= price / area <= 3_000_000):
        return False
    try:
        observed = _utc(row["last_seen"])
    except (ValueError, KeyError):
        return False
    if not now - MAX_SOURCE_AGE <= observed <= now:
        return False
    year = _number(row.get("year_built") or row.get("complex_year_built"))
    if year is not None and (year < 1900 or year > now.astimezone(LOCAL_TIMEZONE).year):
        return False
    return _group(row) is not None or _nearby(row, row)


def sanitized_listing_address(value, price: int, now: datetime) -> tuple[str | None, str]:
    """Keep the source street/house, without a flat number or contact tail."""
    details = build_offer_details({"address": value, "price": price}, "secondary", now=now)
    address = details["address"]
    if address:
        # The shared source sanitizer removes pasted ad text after a dash or
        # newline. A flat identifier is private even when it precedes that tail.
        address = re.split(r"(?:\s*,?\s+|,\s*)(?:кв(?:артира)?\.?|apartment|apt\.?|flat)\s*[№#:]?\s*\d+", address, maxsplit=1, flags=re.I)[0].rstrip(" ,;") or None
        if address and (any(sum(char.isdigit() for char in match[0]) >= 10
                            for match in re.finditer(r"\+?\d[\d\s()\-]+\d", address))
                        or re.search(r"https?://|www\.|@|телефон|whatsapp|ватсап", address, re.I)):
            address = None
    if not address:
        return None, "Точный адрес не указан в источнике."
    # A number in a street name ("38 улица") is not a house. Recognize only
    # an actual trailing house token/list and keep the source prefix verbatim.
    house = r"\d+(?:\s*[/\-]\s*\d+)?[a-zа-яәғқңөұүһі]?"
    houses = re.search(rf"(?P<houses>{house}(?:\s*[,;]\s*{house})+)\s*$", address, re.I)
    if houses:
        return address, "В объявлении указано несколько домов; точный дом нужно уточнить."
    single_house = re.search(rf"(?P<house>{house})\s*$", address, re.I)
    if single_house:
        prefix = address[:single_house.start()].rstrip(" ,;№#")
        if (re.search(r"[a-zа-яәғқңөұүһі]", prefix, re.I)
                and (not re.search(r"(?:р-н|район|Астана|Astana|улица|көше|көшесі)\s*$", prefix, re.I)
                     or re.search(r"\d+\s*(?:улица|көше|көшесі)\s*$", prefix, re.I))):
            return address, "Адрес из объявления."
        return address, "Адрес неполный; точный дом нужно уточнить."
    return address, "Номер дома не указан в объявлении."


def build_daily_picks(rows: list[dict], now: datetime | None = None, limit: int = MAX_PICKS, excluded_ids: set[str] | None = None, *, minimum_factor_count: int = REQUIRED_FACTORS) -> dict:
    """Pure selection and public allowlist; source addresses are sanitized.

    Input approved_photo_urls comes from cached interior fingerprints or a
    matching, fresh normal-browser visual receipt, intersected with current
    photos. minimum_factor_count is for private review discovery; the public
    validator always requires four factors including a discounted price.
    """
    now = _utc(now or datetime.now(timezone.utc))
    if type(minimum_factor_count) is not int or not 1 <= minimum_factor_count <= REQUIRED_FACTORS:
        raise ValueError("Invalid private review factor threshold")
    unique = {}
    for row in rows:
        row = normalize_daily_pick_identity(row)
        if row is None:
            continue
        if not _eligible(row, now):
            continue
        row = dict(row)
        row["rooms"] = int(row["rooms"])
        key = _identity(row)
        if key not in unique or (_utc(row["last_seen"]), str(row["id"])) > (_utc(unique[key]["last_seen"]), str(unique[key]["id"])):
            unique[key] = row
    groups = defaultdict(list)
    nearby_cells = defaultdict(list)
    for row in unique.values():
        groups[_group(row)].append(row)
        cell = _nearby_cell(row)
        if cell is not None:
            nearby_cells[cell].append(row)
    ranked = []
    for row in unique.values():
        # This is a buyer's budget, not a comparable-price truncation. Keep
        # valid expensive peers above in the median's reference population.
        if _number(row["price"]) > MAX_ASKING_PRICE or str(row["id"]) in (excluded_ids or set()):
            continue
        floor, floors = _number(row.get("floor")), _number(row.get("floors_total"))
        if floor is None or floors is None or not floor.is_integer() or not floors.is_integer() or not 1 <= floor <= floors <= 60:
            continue
        photos = list(dict.fromkeys(url for raw in _list(row.get("approved_photo_urls")) if (url := safe_photo_url(raw))))[:3]
        if not photos:
            continue
        area, price = float(row["area"]), int(row["price"])
        local_group = _group(row)
        peers = [p for p in groups[local_group] if _similar(row, p)] if local_group is not None else []
        if len(peers) < MIN_COMPARABLES:
            cell = _nearby_cell(row)
            nearby = (p for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                      for p in nearby_cells.get((cell[0] + dx, cell[1] + dy), ())) if cell is not None else ()
            peers = [p for p in nearby if _similar(row, p) and _nearby(row, p)]
            local_group = None
        if len(peers) < MIN_COMPARABLES:
            continue
        prices = sorted(float(p["price"]) / float(p["area"]) for p in peers)
        reference = median(prices)
        q1, q3 = prices[len(prices) // 4], prices[(len(prices) * 3) // 4]
        if (q3 - q1) / reference > .50:
            continue
        discount = 1 - price / area / reference
        midfloor = 1 < floor < floors
        if not -MAX_PREMIUM <= discount <= MAX_DISCOUNT:
            continue
        percent = round(discount * 100, 1)
        scope = ("в том же доме" if local_group[0] == "house" else "в том же ЖК") if local_group else _NEARBY_SCOPE
        comparison = {"discount_pct": percent, "reference_price_per_m2": round(reference), "scope": scope, "comparable_count": len(peers)}
        facts = selection_facts(row)
        try:
            history = build_offer_details({"price": price, "area": area, "price_events": _list(row.get("price_events"))}, "secondary", now=now)["price_history"]
            address, address_note = sanitized_listing_address(row.get("address"), price, now)
        except (ValueError, TypeError, KeyError):
            continue
        evidence = {"price": price, "area": round(area, 1), "floor": int(floor), "floors_total": int(floors), "price_comparison": comparison,
                    "selection_facts": facts, "price_history": history}
        if percent < 3:
            continue
        reasons = best_apartment_reasons(evidence, now)
        if len(reasons) < minimum_factor_count:
            continue
        cautions = []
        recent_history = [event for event in history["events"] if _utc(event["at"]) >= now-timedelta(days=90)]
        first_price = recent_history[0]["old_price"] if recent_history else None
        cut = (1 - price / first_price if history["status"] == "consistent" and first_price
               and first_price > price and recent_history[-1]["direction"] == "down" else 0)
        if not midfloor:
            cautions.append("Первый этаж." if floor == 1 else "Последний этаж: уточните состояние крыши.")
        if row.get("is_owner") is False:
            cautions.append("Уточните, есть ли комиссия посредника.")
        if row.get("finish_level") in {"rough", "prefinish", "needs_repair"}:
            cautions.append("В объявлении указана отделка под ремонт; расходы на ремонт не включены.")
        elif not row.get("finish_level"):
            cautions.append("Состояние и ремонт уточните у продавца.")
        if local_group is None:
            cautions.append(_NEARBY_CAUTION)
        district = _DISTRICTS.get(row.get("district"), "Астана")
        complex_name = str(row.get("canonical_complex_name") or "")[:100]
        # Only the canonical catalog label is allowed; raw listing complex_name is not.
        item = {
            "id": str(row["id"]), "rank": 0, "source_url": f"https://krisha.kz/a/show/{row['id']}",
            "title": f"{row['rooms']}-комнатная квартира · {round(area, 1):g} м²", "rooms": row["rooms"], "area": round(area, 1),
            "floor": int(floor), "floors_total": int(floors), "complex_name": complex_name, "district": district,
            "complex_identity": public_daily_pick_identity(row),
            "address": address, "address_note": address_note,
            "price": price, "currency": "KZT", "price_per_m2": round(price / area), "photo_urls": photos,
            "reasons": reasons, "cautions": cautions, "observed_at": _utc(row["last_seen"]).isoformat(),
            "price_comparison": comparison, "selection_facts": facts, "price_history": history,
        }
        basis_priority = 2 if local_group and local_group[0] == "house" else 1 if local_group else 0
        ranked.append(((discount, basis_priority, midfloor, cut, _utc(row["last_seen"]).timestamp(), row["id"]), item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    items = [item for _, item in ranked[:max(0, min(limit, MAX_PICKS))]]
    for rank, item in enumerate(items, 1):
        item["rank"] = rank
    return {
        "schema_version": SCHEMA_VERSION, "selection_date": now.astimezone(LOCAL_TIMEZONE).date().isoformat(), "timezone": "Asia/Almaty",
        "generated_at": now.isoformat(), "source_observed_at": max((item["observed_at"] for item in items), default=now.isoformat()),
        "total_candidates": len(ranked), "items": items, "methodology": dict(METHODOLOGY),
    }


def validate_snapshot(snapshot: dict) -> dict:
    """Fail closed and re-project fields before use by a public page."""
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != SCHEMA_VERSION or snapshot.get("timezone") != "Asia/Almaty":
        raise ValueError("Invalid snapshot version or timezone")
    generated = _utc(snapshot["generated_at"])
    if snapshot.get("selection_date") != generated.astimezone(LOCAL_TIMEZONE).date().isoformat() or _utc(snapshot["source_observed_at"]) > generated:
        raise ValueError("Invalid snapshot date")
    items = snapshot.get("items")
    if not isinstance(items, list) or not 0 <= len(items) <= MAX_PICKS:
        raise ValueError("Snapshot of at most 30 items required")
    seen = set()
    safe_items = []
    for rank, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError("Invalid public listing shape")
        lid = item.get("id")
        if not isinstance(lid, str) or not _ID.fullmatch(lid) or lid in seen or item.get("rank") != rank or item.get("source_url") != f"https://krisha.kz/a/show/{lid}" or item.get("currency") != "KZT":
            raise ValueError("Invalid public listing")
        seen.add(lid)
        for field in ("price", "area", "price_per_m2", "rooms", "floor", "floors_total"):
            value = _number(item.get(field))
            if value is None or value <= 0:
                raise ValueError("Invalid public numeric field")
        # Old verified editions can contain luxury listings. Remove those
        # cards independently, retaining the other cards and original receipts.
        if _number(item["price"]) > MAX_ASKING_PRICE:
            continue
        if not generated - MAX_SOURCE_AGE <= _utc(item["observed_at"]) <= generated:
            raise ValueError("Stale source within snapshot")
        if item.get("source_status") != "active" or not generated - timedelta(hours=2) <= _utc(item["source_checked_at"]) <= generated:
            raise ValueError("A recent active source confirmation is required")
        photos = item.get("photo_urls")
        if not isinstance(photos, list) or not 1 <= len(photos) <= 3 or any(safe_photo_url(p) is None for p in photos):
            raise ValueError("Invalid public photo")
        if not isinstance(item.get("reasons"), list) or not 1 <= len(item["reasons"]) <= 4 or any(not isinstance(reason, str) or len(reason) > 200 for reason in item["reasons"]):
            raise ValueError("Invalid public reasons")
        if not isinstance(item.get("cautions"), list) or len(item["cautions"]) > 4 or any(not isinstance(caution, str) or len(caution) > 200 for caution in item["cautions"]):
            raise ValueError("Invalid public cautions")
        comparison = item["price_comparison"]
        if not isinstance(comparison, dict):
            raise ValueError("Invalid comparison")
        count = comparison.get("comparable_count")
        discount = comparison.get("discount_pct")
        reference = comparison.get("reference_price_per_m2")
        scope = comparison.get("scope")
        if (type(count) is not int or count < MIN_COMPARABLES
                or type(discount) not in {int, float} or _number(discount) is None or not -5 <= discount <= 30
                or type(reference) not in {int, float} or _number(reference) is None or reference <= 0
                or not isinstance(scope, str) or scope not in _COMPARISON_SCOPES):
            raise ValueError("Invalid comparison evidence")
        # The displayed percent must follow the stored asking price and median,
        # allowing one decimal place of rounding in both the median and percent.
        actual_m2 = _number(item["price_per_m2"])
        expected_discount = round((1 - actual_m2 / reference) * 100, 1)
        if abs(discount - expected_discount) > .100001:
            raise ValueError("Comparison percent contradicts its price evidence")
        # Public area is rounded to 0.1 m²; allow that precision and the rounded
        # asking price per m², rather than treating unrelated numbers as proof.
        public_area = _number(item["area"])
        asking_price = _number(item["price"])
        if public_area <= .05 or not asking_price / (public_area + .05) - .5 <= actual_m2 <= asking_price / (public_area - .05) + .5:
            raise ValueError("Asking price per square meter contradicts the listing")
        safe_item = {key: item.get(key) for key in _ITEM_FIELDS}
        safe_item["rank"] = len(safe_items) + 1
        safe_item["address"], safe_item["address_note"] = sanitized_listing_address(item.get("address"), int(asking_price), generated)
        safe_item["price_comparison"] = {key: comparison.get(key) for key in _COMPARISON_FIELDS}
        safe_item["selection_facts"] = validate_selection_facts(item.get("selection_facts"), public_area)
        source_facts = item.get("source_facts")
        if source_facts is None:
            source_facts = {}
        if not isinstance(source_facts, dict) or any(
                key != "is_pledged" or type(value) is not bool
                for key, value in source_facts.items()):
            raise ValueError("Invalid confirmed source facts")
        safe_item["source_facts"] = {"is_pledged": True} if source_facts.get("is_pledged") is True else {}
        raw_history = item.get("price_history")
        if raw_history is not None:
            if not isinstance(raw_history, dict) or raw_history.get("baseline_unknown") is not False:
                raise ValueError("Invalid daily price history")
            safe_item["price_history"] = price_history(raw_history.get("events"), asking_price, now=generated)
        else:
            safe_item["price_history"] = None
        # Refresh copy for already verified snapshots without reranking or
        # changing their source confirmations, observations, or publication date.
        if item.get("complex_identity") is not None:
            safe_item["complex_identity"] = validate_public_daily_identity(item["complex_identity"])
            safe_item["complex_name"] = safe_item["complex_identity"]["canonical_name"] or ""
            if (scope == "в том же доме" and safe_item["complex_identity"]["house_id"] is None
                    or scope == "в том же ЖК" and safe_item["complex_identity"]["canonical_id"] is None):
                raise ValueError("Comparison scope contradicts catalog identity")
        else:
            # Older editions must not silently reassert an unreviewed catalog
            # label. A controlled read-only identity refresh can enrich them.
            continue
        safe_item["reasons"] = best_apartment_reasons(safe_item, generated)
        if len(safe_item["reasons"]) != REQUIRED_FACTORS:
            continue
        safe_item["cautions"] = [_NEARBY_CAUTION if text == "Координаты приблизительные; расположение нужно уточнить." else text for text in item["cautions"]]
        if scope == _NEARBY_SCOPE and _NEARBY_CAUTION not in safe_item["cautions"]:
            if len(safe_item["cautions"]) >= 4:
                raise ValueError("Nearby comparison requires its location caveat")
            safe_item["cautions"].append(_NEARBY_CAUTION)
        safe_items.append(safe_item)
    safe = {key: snapshot.get(key) for key in _SNAPSHOT_FIELDS}
    safe["items"] = safe_items
    policy = snapshot.get("publication_policy")
    if policy is not None:
        policy_fields = ("name", "target_count", "minimum_count", "published_count", "required_strong_factors", "minimum_discount_pct", "reason", "explicit", "evaluated_at")
        if (not isinstance(policy, dict) or any(key not in policy for key in policy_fields)
                or policy.get("name") != "discounted_four_factors" or policy.get("explicit") is not True
                or type(policy.get("target_count")) is not int or policy["target_count"] != MAX_PICKS
                or type(policy.get("minimum_count")) is not int or policy["minimum_count"] != MIN_PICKS
                or type(policy.get("published_count")) is not int or not MIN_PICKS <= policy["published_count"] <= MAX_PICKS
                or policy["published_count"] != len(safe_items) or len(safe_items) != len(items)
                or type(policy.get("required_strong_factors")) is not int or policy["required_strong_factors"] != REQUIRED_FACTORS
                or type(policy.get("minimum_discount_pct")) is not int or policy["minimum_discount_pct"] != 3
                or policy.get("reason") != "discount_and_four_factors" or _utc(policy.get("evaluated_at")) != generated):
            raise ValueError("Invalid explicit daily publication policy")
        safe["publication_policy"] = {key: policy[key] for key in policy_fields}
    if not isinstance(snapshot.get("methodology"), dict):
        raise ValueError("Invalid methodology shape")
    safe["methodology"] = dict(METHODOLOGY)
    return safe


def snapshot_status(snapshot: dict | None, now: datetime | None = None) -> dict:
    now = _utc(now or datetime.now(timezone.utc))
    if snapshot is None:
        return {"is_stale": False, "age_hours": None, "status": "unavailable"}
    generated = _utc(snapshot["generated_at"])
    # 08:00 publishing; yesterday's snapshot is normal until today's refresh.
    local = now.astimezone(LOCAL_TIMEZONE)
    expected_date = (local - timedelta(days=1)).date() if local.hour < 8 else local.date()
    stale = generated > now or snapshot["selection_date"] < expected_date.isoformat() or now - generated > timedelta(hours=26)
    return {"is_stale": stale, "age_hours": round(max(0, (now - generated).total_seconds() / 3600), 1), "status": "stale" if stale else "current"}


def load_daily_picks(path: Path | None = None) -> dict | None:
    try:
        return validate_snapshot(json.loads((path or snapshot_path()).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def publish_snapshot(snapshot: dict, path: Path | None = None) -> None:
    """Atomic, same-filesystem publish. Failed writes retain the last good file."""
    safe = validate_snapshot(snapshot)
    if len(safe["items"]) < MIN_PICKS:
        raise ValueError("Fewer than ten qualifying apartments cannot replace a verified snapshot")
    path = path or snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(safe, output, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
