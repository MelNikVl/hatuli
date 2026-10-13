"""Public, read-only primary/secondary comparison on the scanner's axial grid.

Asking prices are observed offers, not sales or investment returns. Direct
developer units and Krisha primary listings must not be merged as two homes.
Only the offline publisher reads PostgreSQL; public requests read a validated
local file containing an explicit public projection.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from statistics import median
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from bot.core.hexgrid import hex_id as point_hex
from bot.core.comparison_offer_details import DETAIL_FIELDS, build_offer_details, validate_offer_details

SCHEMA_VERSION = 3
LOCAL_TIMEZONE = ZoneInfo("Asia/Almaty")
MAX_SOURCE_AGE = timedelta(hours=72)
MIN_COMPARABLES = 5
AREA_BAND = .15
MAX_RELATIVE_IQR = .50
PRIMARY_SOURCES = {"bi_group": "BI Group", "bazis": "BAZIS", "sensata": "Sensata", "nak": "NAK", "orda_invest": "Orda Invest", "svoydom": "Svoydom"}
SCOPE_RADII = {"hex": 0, "ring1": 1, "ring2": 2, "ring4": 4, "ring8": 8, "city": None}
SCOPE_LABELS = {"hex": "Один гексагон", "ring1": "Гексагон и 6 соседей", "ring2": "19 гексагонов", "ring4": "61 гексагон", "ring8": "217 гексагонов", "city": "Вся Астана"}
_HEX = re.compile(r"(-?\d{1,5}):(-?\d{1,5})\Z")
_FINISH = {"rough": "Черновая", "prefinish": "Предчистовая", "finished": "Чистовая", "renovated": "С ремонтом", "furnished": "С мебелью", "designer": "Дизайнерский ремонт", "needs_repair": "Требуется ремонт"}
_STAGE = {"Строящийся": "under_construction", "Сдан в эксплуатацию": "commissioned"}
_STAGE_LABEL = {"under_construction": "Строится по данным каталога", "commissioned": "В каталоге указан ввод в эксплуатацию", "unknown": "Статус ввода не подтверждён"}
_OFFER_FIELDS = ("id", "market_type", "complex_id", "complex_name", "hex_id", "rooms", "area", "price", "price_per_m2", "floor", "floors_total", "finish_level", "finish_label", "build_status", "build_status_label", "completion_year", "completion_quarter", "housing_class", "source_label", "url", "observed_at", "geo_source", "geo_quality") + DETAIL_FIELDS
_CATALOG_GEO_SOURCES = {"listings", "homsters", "korter", "developer", "nominatim", "unknown"}
_GEO_QUALITY = {"listings": "listing_centroid", "homsters": "source_project_point", "korter": "source_project_point",
                "developer": "source_project_point", "nominatim": "geocoded_project_point", "unknown": "unknown"}


def snapshot_path() -> Path:
    return Path(os.environ.get("HATULI_MARKET_COMPARISON_PATH") or Path(__file__).resolve().parents[2] / "var" / "market-comparison.json")


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Timezone-aware date required")
    return value.astimezone(timezone.utc)


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (ValueError, TypeError):
        return None
    return n if math.isfinite(n) else None


def _int(value, low: int, high: int) -> int | None:
    n = _number(value)
    return int(n) if n is not None and n.is_integer() and low <= n <= high else None


def valid_hex(value: str) -> bool:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        return False
    q, r = map(int, value.split(":"))
    return abs(q) <= 2000 and abs(r) <= 2000


def hex_distance(first: str, second: str) -> int:
    q1, r1 = map(int, first.split(":"))
    q2, r2 = map(int, second.split(":"))
    return max(abs(q1 - q2), abs(r1 - r2), abs((q1 + r1) - (q2 + r2)))


def _safe_secondary_url(value, listing_id: str) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        p = urlsplit(value)
        if (p.scheme == "https" and p.hostname in {"krisha.kz", "www.krisha.kz"} and not p.username
                and not p.password and p.port is None and not p.query and not p.fragment
                and p.path.rstrip("/") == f"/a/show/{listing_id}"):
            return f"https://krisha.kz/a/show/{listing_id}"
    except ValueError:
        pass
    return None


def _public_offer(row: dict, market: str, now: datetime, edge_m: float) -> dict | None:
    """Keep basic public fields only; discard all private fields and coordinates."""
    if row.get("comparison_only"):
        return None
    rooms, price = _int(row.get("rooms"), 1, 6), _int(row.get("price"), 5_000_000, 600_000_000)
    area = _number(row.get("area"))
    lat, lon = _number(row.get("lat")), _number(row.get("lon"))
    if rooms is None or price is None or area is None or not 20 <= area <= 300:
        return None
    if lat is None or lon is None or not (50.70 <= lat <= 51.55 and 70.80 <= lon <= 72.10):
        return None
    price_m2 = price / area
    if not 100_000 <= price_m2 <= 3_000_000:
        return None
    try:
        observed = _utc(row.get("last_seen_at") if market == "primary" else row.get("last_seen"))
    except (ValueError, TypeError, AttributeError):
        return None
    if not now - MAX_SOURCE_AGE <= observed <= now:
        return None
    floor, floors_total = _int(row.get("floor"), 1, 60), _int(row.get("floors_total"), 1, 60)
    if floor is not None and floors_total is not None and floor > floors_total:
        return None
    cid = _int(row.get("complex_id"), 1, 2_147_483_647)
    if market == "primary":
        uid = _int(row.get("id"), 1, 2_147_483_647)
        source = row.get("source")
        if row.get("status") != "available" or source not in PRIMARY_SOURCES or cid is None or uid is None:
            return None
        identifier, url, source_label = f"newbuild:{uid}", f"/complex/{cid}", PRIMARY_SOURCES[source]
        finish = None  # Unit-level finish is absent from the structured source.
        geo_source = row.get("coords_source") if row.get("coords_source") in _CATALOG_GEO_SOURCES else "unknown"
        geo_quality = _GEO_QUALITY[geo_source]
    else:
        identifier = str(row.get("id", ""))
        url = _safe_secondary_url(row.get("url"), identifier)
        if (not re.fullmatch(r"\d{5,20}", identifier) or not url or row.get("market_type") != "secondary"
                or row.get("is_active") is not True or row.get("is_duplicate") or row.get("dup_needs_review")
                or row.get("linked_primary_unit")):
            return None
        source_label = "Krisha · вторичка"
        geo_source, geo_quality = "krisha_listing", "approximate_listing_point"
        finish = row.get("finish_level") if row.get("finish_level") in _FINISH else None
        year = _int(row.get("year_built"), 1800, 2200)
        if year is not None and year > now.astimezone(LOCAL_TIMEZONE).year:
            return None
    name = row.get("complex_name")
    name = name.strip()[:100] if cid is not None and isinstance(name, str) and name.strip() else None
    stage = _STAGE.get(row.get("build_status"), "unknown") if market == "primary" else "unknown"
    housing_class = row.get("housing_class")
    housing_class = housing_class.strip()[:50] if isinstance(housing_class, str) and housing_class.strip() else None
    return {
        "id": identifier, "market_type": market, "complex_id": cid, "complex_name": name,
        "hex_id": point_hex(lat, lon, edge_m), "rooms": rooms, "area": round(area, 2),
        "price": price, "price_per_m2": round(price_m2),
        "floor": floor, "floors_total": floors_total,
        "finish_level": finish, "finish_label": _FINISH.get(finish, "Отделка не подтверждена"),
        "build_status": stage, "build_status_label": _STAGE_LABEL[stage],
        "completion_year": _int(row.get("completion_year"), 2000, 2200),
        "completion_quarter": _int(row.get("completion_quarter"), 1, 4),
        "housing_class": housing_class, "source_label": source_label, "url": url,
        "observed_at": observed.isoformat(), "geo_source": geo_source, "geo_quality": geo_quality,
        **build_offer_details(row, market, now=now),
    }


def build_market_snapshot(primary_rows: list[dict], secondary_rows: list[dict], *, now: datetime | None = None,
                          edge_m: float = 50, coverage: dict | None = None) -> dict:
    now = _utc(now or datetime.now(timezone.utc))
    edge = _number(edge_m)
    if edge is None or not 25 <= edge <= 500:
        raise ValueError("Invalid scanner hex edge")
    offers, seen = [], set()
    # Known property spine suppresses repeated advertisements. It is not public.
    latest_secondary = sorted(secondary_rows, key=lambda r: str(r.get("last_seen") or ""), reverse=True)
    properties = set()
    for market, rows in (("primary", primary_rows), ("secondary", latest_secondary)):
        for row in rows:
            offer = _public_offer(row, market, now, edge)
            if not offer or (market, offer["id"]) in seen:
                continue
            property_id = row.get("property_id") if market == "secondary" else None
            if property_id is not None and property_id in properties:
                continue
            if property_id is not None:
                properties.add(property_id)
            offers.append(offer)
            seen.add((market, offer["id"]))
    counts = Counter(o["market_type"] for o in offers)
    safe_coverage = {str(k): int(v) for k, v in (coverage or {}).items()
                     if k in {"krisha_primary_recent", "primary_available_recent", "secondary_recent", "primary_stale_available"}
                     and _int(v, 0, 10_000_000) is not None}
    listing_details = []
    for row in secondary_rows:
        if not row.get("public_comparison_listing"):
            continue
        identifier = str(row.get("id", ""))
        if not re.fullmatch(r"\d{5,20}", identifier) or row.get("market_type") != "secondary":
            raise ValueError("Invalid public comparison listing")
        listing_details.append({"id": identifier, "current_price": row["price"], "area": row.get("area"), "rooms": row.get("rooms"),
                                **build_offer_details(row, "secondary", now=now)})
    return {
        "schema_version": SCHEMA_VERSION, "generated_at": now.isoformat(), "timezone": "Asia/Almaty",
        "edge_m": edge, "grid": "scanner_axial", "freshness_hours": 72,
        "summary": {"primary_count": counts["primary"], "secondary_count": counts["secondary"],
                    "observed_hexes": len({o["hex_id"] for o in offers}), "coverage": safe_coverage},
        "offers": offers,
        "listing_details": listing_details,
    }


def _validated_snapshot(raw: dict) -> dict:
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION or raw.get("grid") != "scanner_axial":
        raise ValueError("Unsupported market snapshot")
    generated = _utc(raw["generated_at"])
    edge = _number(raw.get("edge_m"))
    if edge is None or not 25 <= edge <= 500 or raw.get("timezone") != "Asia/Almaty":
        raise ValueError("Invalid grid metadata")
    rows = raw.get("offers")
    if not isinstance(rows, list) or len(rows) > 100_000:
        raise ValueError("Invalid public offer collection")
    offers, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or not all(k in row for k in _OFFER_FIELDS if k not in {"selection_facts", "rental_estimate"}):
            raise ValueError("Invalid public offer")
        market, identifier = row["market_type"], row["id"]
        if market not in {"primary", "secondary"} or not valid_hex(row["hex_id"]):
            raise ValueError("Invalid market or cell")
        if not isinstance(identifier, str) or (market, identifier) in seen:
            raise ValueError("Duplicate offer identifier")
        price, area, rooms = _int(row["price"], 5_000_000, 600_000_000), _number(row["area"]), _int(row["rooms"], 1, 6)
        if price is None or area is None or not 20 <= area <= 300 or rooms is None:
            raise ValueError("Invalid public price or size")
        price_per_m2 = _number(row["price_per_m2"])
        if price_per_m2 is None or abs(price / area - price_per_m2) > 100 or not 100_000 <= price / area <= 3_000_000:
            raise ValueError("Price per metre does not match offer")
        observed = _utc(row["observed_at"])
        if not generated - MAX_SOURCE_AGE <= observed <= generated:
            raise ValueError("Source observation outside publication window")
        cid = _int(row["complex_id"], 1, 2_147_483_647)
        if row["complex_id"] is not None and cid is None:
            raise ValueError("Invalid complex identifier")
        for field in ("floor", "floors_total"):
            if row[field] is not None and _int(row[field], 1, 60) is None:
                raise ValueError("Invalid floor")
        if row["floor"] is not None and row["floors_total"] is not None and row["floor"] > row["floors_total"]:
            raise ValueError("Floor exceeds building")
        for field, low, high in (("completion_year", 2000, 2200), ("completion_quarter", 1, 4)):
            if row[field] is not None and _int(row[field], low, high) is None:
                raise ValueError("Invalid planned completion")
        if market == "primary":
            if not re.fullmatch(r"newbuild:\d{1,10}", identifier) or cid is None or row["url"] != f"/complex/{cid}":
                raise ValueError("Invalid public primary target")
            if row["source_label"] not in PRIMARY_SOURCES.values() or row["finish_level"] is not None:
                raise ValueError("Unverified developer/finish")
            if row["geo_source"] not in _CATALOG_GEO_SOURCES or row["geo_quality"] != _GEO_QUALITY[row["geo_source"]]:
                raise ValueError("Unverified catalogue coordinate quality")
        elif (not re.fullmatch(r"\d{5,20}", identifier) or not _safe_secondary_url(row["url"], identifier)
              or row["source_label"] != "Krisha · вторичка" or row["geo_source"] != "krisha_listing"
              or row["geo_quality"] != "approximate_listing_point"):
            raise ValueError("Invalid public secondary target")
        stage, finish = row["build_status"], row["finish_level"]
        if stage not in _STAGE_LABEL or (finish is not None and finish not in _FINISH):
            raise ValueError("Invalid public status")
        safe = {k: row.get(k) for k in _OFFER_FIELDS}
        safe.update(price=price, area=area, rooms=rooms, price_per_m2=round(price_per_m2))
        safe.update(validate_offer_details(row, market, price, now=generated))
        for field, limit in (("complex_name", 100), ("housing_class", 50)):
            if safe[field] is not None and (not isinstance(safe[field], str) or len(safe[field]) > limit):
                raise ValueError("Invalid public text")
        safe["finish_label"], safe["build_status_label"] = _FINISH.get(finish, "Отделка не подтверждена"), _STAGE_LABEL[stage]
        offers.append(safe)
        seen.add((market, identifier))
    counts = Counter(o["market_type"] for o in offers)
    listing_details, detail_ids = [], set()
    if not isinstance(raw.get("listing_details"), list) or len(raw["listing_details"]) > 30:
        raise ValueError("Invalid public comparison details")
    for row in raw["listing_details"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not re.fullmatch(r"\d{5,20}", row["id"]) or row["id"] in detail_ids:
            raise ValueError("Invalid public comparison detail identifier")
        current = _int(row.get("current_price"), 1, 10_000_000_000)
        if current is None:
            raise ValueError("Invalid comparison current price")
        area = _number(row.get("area"))
        if area is not None and not 20 <= area <= 300:
            raise ValueError("Invalid public comparison detail area")
        rooms = _int(row.get("rooms"), 1, 6)
        if row.get("rooms") is not None and rooms is None:
            raise ValueError("Invalid public comparison detail rooms")
        listing_details.append({"id": row["id"], "current_price": current, "area": area, "rooms": rooms,
                                **validate_offer_details(row, "secondary", current, now=generated, area=area)})
        detail_ids.add(row["id"])
    coverage = raw.get("summary", {}).get("coverage", {})
    coverage = {k: v for k, v in coverage.items() if k in {"krisha_primary_recent", "primary_available_recent", "secondary_recent", "primary_stale_available"} and _int(v, 0, 10_000_000) is not None}
    return {"schema_version": SCHEMA_VERSION, "generated_at": generated.isoformat(), "timezone": "Asia/Almaty",
            "edge_m": edge, "grid": "scanner_axial", "freshness_hours": 72,
            "summary": {"primary_count": counts["primary"], "secondary_count": counts["secondary"],
                        "observed_hexes": len({o["hex_id"] for o in offers}), "coverage": coverage}, "offers": offers,
            "listing_details": listing_details}


@lru_cache(maxsize=2)
def _read_snapshot(path: str, mtime: int, size: int) -> dict:
    del mtime
    if size > 40_000_000:
        raise ValueError("Market snapshot too large")
    return _validated_snapshot(json.loads(Path(path).read_text(encoding="utf-8")))


def load_market_snapshot(path: Path | None = None) -> dict:
    path = path or snapshot_path()
    try:
        stat = path.stat()
        return _read_snapshot(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"schema_version": SCHEMA_VERSION, "generated_at": None, "timezone": "Asia/Almaty",
                "edge_m": 50, "grid": "scanner_axial", "freshness_hours": 72,
                "summary": {"primary_count": 0, "secondary_count": 0, "observed_hexes": 0, "coverage": {}}, "offers": [], "listing_details": []}


def publish_market_snapshot(snapshot: dict, path: Path | None = None) -> None:
    safe = _validated_snapshot(snapshot)
    if not safe["summary"]["primary_count"] or not safe["summary"]["secondary_count"]:
        raise ValueError("Both markets required; retain the previous snapshot")
    path = path or snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(safe, output, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            output.flush()
            if os.fstat(output.fileno()).st_size > 40_000_000:
                raise ValueError("Market snapshot too large; retain the previous snapshot")
            os.fsync(output.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _date_label(value: str | None) -> str:
    return _utc(value).astimezone(LOCAL_TIMEZONE).strftime("%d.%m.%Y, %H:%M") if value else "Нет наблюдений"


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def _stats(offers: list[dict], market: str) -> dict:
    prices = [o["price_per_m2"] for o in offers]
    p25, p75 = _quantile(prices, .25), _quantile(prices, .75)
    med = median(prices) if prices else None
    finishes = Counter(o["finish_level"] or "unknown" for o in offers)
    stages = Counter(o["build_status"] for o in offers)
    geo_quality = Counter(o["geo_quality"] for o in offers)
    newest = max((o["observed_at"] for o in offers), default=None)
    oldest = min((o["observed_at"] for o in offers), default=None)
    advantages, cautions = [], []
    if market == "primary":
        if offers:
            advantages.append("Цена конкретного юнита из прайса застройщика.")
        if stages["commissioned"]:
            advantages.append(f"У {stages['commissioned']} вариантов каталог указывает ввод дома в эксплуатацию; документы нужно подтвердить.")
        if stages["under_construction"]:
            cautions.append(f"{stages['under_construction']} вариантов в строящихся домах: возможны ожидание ключей, перенос сроков и аренда на время ожидания.")
        if stages["unknown"]:
            cautions.append(f"У {stages['unknown']} вариантов статус ввода не подтверждён. Плановый год сдачи не доказывает готовность.")
        if offers:
            cautions.append("Отделка юнита, ремонт, меблировка и схема оплаты не подтверждены: базовая цена не равна сумме для заселения.")
            cautions.append("Координата относится к ЖК, а не к корпусу конкретного юнита. Границы маленьких гексов не подтверждены.")
        if geo_quality["listing_centroid"]:
            cautions.append(f"{geo_quality['listing_centroid']} вариантов привязаны к центру объявлений ЖК; это оценочная точка, не подтверждённый адрес корпуса.")
        if geo_quality["unknown"]:
            cautions.append(f"У {geo_quality['unknown']} вариантов источник координаты ЖК не установлен.")
    else:
        ready = sum(finishes[k] for k in ("finished", "renovated", "furnished", "designer"))
        if ready:
            advantages.append(f"{ready} вариантов с указанной готовой отделкой; состояние нужно проверить на просмотре.")
        if offers:
            advantages.append("У готовой квартиры можно осмотреть состояние, дом и окружение до покупки; готовность объекта нужно подтвердить.")
            cautions.append("Проверить право собственности, обременения, долги, перепланировку и состояние инженерных сетей.")
        if finishes["unknown"]:
            cautions.append(f"У {finishes['unknown']} вариантов отделка неизвестна: стоимость ремонта не учтена.")
        if finishes["rough"] + finishes["prefinish"] + finishes["needs_repair"]:
            cautions.append("Часть вторички требует ремонта; готовность к заселению нельзя считать общей для всей группы.")
    if offers:
        cautions.append("Наличие и окончательную цену нужно подтвердить у источника: это недавние наблюдения, не бронирование.")
    return {"count": len(offers), "median_per_m2": round(med) if med else None,
            "median_total": round(median(o["price"] for o in offers)) if offers else None,
            "p25_per_m2": round(p25) if p25 else None, "p75_per_m2": round(p75) if p75 else None,
            "relative_iqr": (p75 - p25) / med if med else None,
            "complex_count": len({o["complex_id"] for o in offers if o["complex_id"] is not None}),
            "finish_known_count": len(offers) - finishes["unknown"], "finish_counts": dict(finishes),
            "stage_counts": dict(stages), "housing_class_known_count": sum(bool(o["housing_class"]) for o in offers),
            "geo_quality_counts": dict(geo_quality),
            "observed_at": newest, "observed_at_label": _date_label(newest), "oldest_observed_at": oldest,
            "advantages": advantages, "cautions": cautions}


def _default_center(offers: list[dict]) -> str:
    grouped = defaultdict(Counter)
    for o in offers:
        grouped[o["hex_id"]][o["market_type"]] += 1
    if not grouped:
        return "0:0"
    def score(hid):
        direct = grouped[hid]
        p = s = 0
        for other, counts in grouped.items():
            if hex_distance(hid, other) <= 1:
                p += counts["primary"]
                s += counts["secondary"]
        return (min(direct["primary"], direct["secondary"]), min(p, s), min(p, 100) + min(s, 100), hid)
    # Only primary-containing cells can provide useful starting comparisons.
    candidates = [hid for hid, counts in grouped.items() if counts["primary"]]
    return max(candidates or grouped, key=score)


def compare_market(snapshot: dict, hex_id: str | None = None, rooms: int = 2, area: float = 60,
                   scope: str = "hex", *, now: datetime | None = None) -> dict:
    rooms = _int(rooms, 1, 6) or 2
    area = _number(area)
    area = area if area is not None and 20 <= area <= 300 else 60.0
    scope = scope if scope in SCOPE_RADII else "hex"
    now = _utc(now or datetime.now(timezone.utc))
    all_offers = snapshot.get("offers", [])
    matching = [o for o in all_offers if o["rooms"] == rooms and area * (1 - AREA_BAND) <= o["area"] <= area * (1 + AREA_BAND)]
    center = hex_id if valid_hex(hex_id) else _default_center(matching)
    generated = snapshot.get("generated_at")
    stale = not generated or now - _utc(generated) > timedelta(hours=26) or _utc(generated) > now + timedelta(minutes=5)
    tiers, stats_by_scope, rows_by_scope = [], {}, {}
    for key, radius in SCOPE_RADII.items():
        rows = matching if radius is None else [o for o in matching if hex_distance(center, o["hex_id"]) <= radius]
        primary = [o for o in rows if o["market_type"] == "primary"]
        secondary = [o for o in rows if o["market_type"] == "secondary"]
        stats = {"primary": _stats(primary, "primary"), "secondary": _stats(secondary, "secondary")}
        sufficient = len(primary) >= MIN_COMPARABLES and len(secondary) >= MIN_COMPARABLES
        dispersion_ok = sufficient and all(stats[m]["relative_iqr"] <= MAX_RELATIVE_IQR for m in stats)
        # Every available primary coordinate is a project point, often the
        # centroid of advertisements. No current pipeline verifies unit/building
        # locations at this resolution. Never turn that uncertainty into a
        # confident 1/7/19-cell price conclusion, even with many observations.
        micro_geo_unknown = radius is not None and radius <= 2 and bool(primary)
        comparable = bool(sufficient and dispersion_ok and not stale and not micro_geo_unknown)
        delta = round((stats["secondary"]["median_per_m2"] - stats["primary"]["median_per_m2"]) / stats["secondary"]["median_per_m2"] * 100, 1) if comparable else None
        confidence = ("Снимок устарел; обновление необходимо до вывода о цене." if stale
                      else "Точность координаты ЖК до корпуса не подтверждена. На масштабе 1, 7 и 19 гексов ценовую дельту не показываем; количества и цены служат ориентиром." if micro_geo_unknown
                      else "Недостаточно аналогов: нужно не менее 5 вариантов каждого рынка." if not sufficient
                      else "Большой разброс цен: смешанная выборка не даёт надёжного сравнения." if not dispersion_ok
                      else "Городской срез смешивает локации и классы домов. Базовые цены сопоставлены по комнатам и площади ±15%; это ориентир, не аналог конкретной квартиры." if key == "city"
                      else "Широкий срез по приблизительным точкам ЖК и объявлений: границы зоны не подтверждены до корпуса. Базовые цены сопоставлены по комнатам и площади ±15%; отделка и класс могут различаться.")
        if comparable and delta and abs(delta) >= 1:
            lower = "primary" if delta > 0 else "secondary"
            pct = abs(delta) if delta > 0 else abs(delta) / (1 + abs(delta) / 100)
            stats[lower]["advantages"].insert(0, f"Базовая медиана за м² ниже на {pct:.1f}% в выбранной группе. Это не сравнение полной стоимости заселения.")
        if key == "city":
            for group in stats.values():
                group["cautions"].append("Городская выборка смешивает локации, классы домов и стадии строительства; она служит ориентиром, не аналогом конкретной квартиры.")
        href = "/compare?" + urlencode({"hex": center, "rooms": rooms, "area": f"{area:g}", "scope": key})
        tiers.append({"key": key, "scope": key, "label": SCOPE_LABELS[key],
                      "cells_count": len({o["hex_id"] for o in all_offers}) if radius is None else 1 + 3 * radius * (radius + 1),
                      "primary_count": len(primary), "secondary_count": len(secondary), "delta_pct": delta,
                      "comparable": comparable, "confidence_reason": confidence, "geo_micro_confirmed": False, "href": href})
        stats_by_scope[key], rows_by_scope[key] = stats, rows
    seen_complexes, locations = set(), []
    for o in sorted((o for o in all_offers if o["market_type"] == "primary" and o["complex_name"]), key=lambda x: x["complex_name"]):
        if o["complex_id"] not in seen_complexes:
            locations.append({"hex_id": o["hex_id"], "name": o["complex_name"], "complex_id": o["complex_id"], "geo_source": o["geo_source"], "geo_quality": o["geo_quality"]})
            seen_complexes.add(o["complex_id"])
    names = sorted({o["complex_name"] for o in all_offers if o["market_type"] == "primary" and o["hex_id"] == center and o["complex_name"]})
    if not names:
        names = sorted({o["complex_name"] for o in all_offers if o["hex_id"] == center and o["complex_name"]})
    selected_label = ", ".join(names[:2]) if names else f"Гексагон {center}"
    apartments = sorted((o for o in rows_by_scope[scope] if o["market_type"] == "primary"), key=lambda o: (abs(o["area"] - area), o["price_per_m2"], o["id"]))[:30]
    apartments = [{**o, "observed_at_label": _date_label(o["observed_at"]),
                   "cautions": [o["build_status_label"], "Отделка и условия оплаты нужно подтвердить у застройщика."]} for o in apartments]
    notices = ["Сетка совпадает со сканером Hatuli. Координаты объявлений и точка ЖК могут быть приблизительными: маленький гексагон не доказывает расположение конкретного корпуса.",
               "Первичка — конкретные доступные юниты из прайсов застройщиков. Первичные объявления Krisha показаны в покрытии отдельно и не добавлены второй раз.",
               "Срез включает предложения, наблюдавшиеся в течение 72 часов перед публикацией снимка. Это базовые цены объявлений, не состоявшиеся сделки и не прогноз доходности.",
               "Без подтверждённых расходов на ремонт, аренду до ключей, налогов и сборов нельзя объявить один рынок выгоднее по полной стоимости."]
    if stale:
        notices.insert(0, "Снимок ещё не готов или устарел. Ценовые выводы временно скрыты.")
    tier = next(t for t in tiers if t["key"] == scope)
    return {"updated_at": generated, "updated_at_label": _date_label(generated), "stale": stale,
            "selected_hex": center, "selected_label": selected_label, "edge_m": snapshot.get("edge_m", 50),
            "scope": scope, "scope_label": SCOPE_LABELS[scope], "rooms": rooms, "area": area,
            "area_min": round(area * (1 - AREA_BAND), 1), "area_max": round(area * (1 + AREA_BAND), 1),
            "locations": locations, "room_options": [1, 2, 3, 4, 5, 6], "tiers": tiers,
            "stats": stats_by_scope[scope], "comparable": tier["comparable"], "delta_pct": tier["delta_pct"],
            "confidence_reason": tier["confidence_reason"], "notices": notices, "apartments": apartments,
            "summary": snapshot.get("summary", {})}


def public_primary_offer(snapshot: dict, unit_id: int | str) -> dict | None:
    """Find any validated developer unit for its individual comparison page."""
    uid = _int(unit_id, 1, 2_147_483_647)
    if uid is None:
        return None
    identifier = f"newbuild:{uid}"
    for offer in snapshot.get("offers", []):
        if offer["market_type"] == "primary" and offer["id"] == identifier:
            return {**offer, "observed_at_label": _date_label(offer["observed_at"]),
                    "cautions": [offer["build_status_label"], "Отделка, сроки передачи ключей и условия оплаты нужно подтвердить у застройщика."]}
    return None
