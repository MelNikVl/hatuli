"""
Общий пайплайн парсеров раздела "Новостройки" (см. migrations/041_newbuild.sql,
042_newbuild_developer_id.sql) — застройщик/ЖК/юнит upsert, геокодинг,
диф "пропал из выдачи -> sold", история цены. Написан один раз в
bi_group_import.py, вынесен сюда, чтобы каждый следующий сайт-специфичный
скрипт (sensata_import.py, orda_invest_import.py, bazis_import.py,
nak_import.py) не копипастил ~150 строк DB-логики — только парсит сайт и
складывает результат в ComplexData/UnitData ниже.

Единица данных с сайта застройщика (не привязана к конкретному API) —
ровно то общее, что есть у всех: id/имя/адрес ЖК + список юнитов с
комн/площадь/этаж/цена/статус/фото планировки.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bot.core.geo import in_astana_bbox

log = logging.getLogger("newbuild_common")

_DISTRICT_RE = re.compile(r"район\s+([^,]+)", re.I)


@dataclass
class UnitData:
    source_unit_id: str
    rooms: int | None = None
    area: float | None = None
    floor: int | None = None
    floors_total: int | None = None
    price: int | None = None
    price_per_m2: float | None = None
    building: str | None = None
    section: str | None = None
    layout_photo_url: str | None = None
    photos: list[str] | None = None
    is_available: bool = True  # False = сейчас в процессе бронирования у застройщика
    deadline: datetime | None = None  # для year/quarter конкретно этого юнита (корпуса)
    raw: dict = field(default_factory=dict)


@dataclass
class ComplexData:
    source_id: str
    name: str
    address: str | None
    housing_class: str | None
    units: list[UnitData]
    lat: float | None = None  # если источник отдаёт координаты сам (NAK) — используем их, без Nominatim
    lon: float | None = None
    # Фото обложки ЖК + краткое описание — если источник отдаёт их на своей
    # странице проекта (не все source-скрипты сейчас их парсят, см. задачу
    # "фото/описание для новостроек" 2026-08-09); COALESCE в ensure_complex
    # ниже — не затираем то, что уже заполнено вручную из /admin.
    photo_url: str | None = None
    description: str | None = None


def _quarter(dt: datetime) -> int:
    return (dt.month - 1) // 3 + 1


def parse_deadline_iso(s: str | None) -> datetime | None:
    """'2027-04-23' / '2026-08-31T17:59:31.000+00:00' -> datetime, для
    UnitData.deadline. Общий для всех источников формат дат ISO 8601."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


_ADDR_STRIP_RE = [
    (re.compile(r"^РК,?\s*", re.I), ""),
    (re.compile(r"г\.?\s*Астана\s*,?|Астана\s*г\.?\s*,?", re.I), ""),
    (re.compile(r"район\s+[^,]+,?\s*", re.I), ""),
    (re.compile(r",?\s*(уч\.|участок)\s*№?\s*\d+[А-ЯA-Z]?\s*$", re.I), ""),
]


def clean_address_for_geocode(address: str) -> str:
    """Nominatim не резолвит полный сырой адрес с участком ещё не
    существующего дома ("РК, Астана г., район Нұра, проспект Ұлы дала,
    уч. 18" -> []) — урезаем до улицы, город+"Казахстан" добавляет сам
    geo.geocode. Проверено вручную на BI Group — попадает уверенно на
    нужную улицу, этого достаточно для маркера на карте города."""
    s = address
    for pattern, repl in _ADDR_STRIP_RE:
        s = pattern.sub(repl, s)
    return s.strip(" ,")


async def geocode_complex(cid: int, address: str) -> None:
    """Координаты нужны только один раз на ЖК (для маркера на карте) —
    вызывающая сторона решает, когда именно (lat/lon ещё NULL)."""
    from bot.core.geo import geocode
    from bot.db.pg import execute

    query = clean_address_for_geocode(address)
    try:
        coords = await geocode(query, city="astana")
    except Exception as e:
        log.warning("геокодинг не удался для %r: %s", query, e)
        return
    if coords:
        await execute("UPDATE complexes SET lat = $2, lon = $3 WHERE id = $1",
                       cid, coords[0], coords[1])
    else:
        log.warning("геокодинг: не найдено для %r (исходно %r)", query, address)


async def ensure_developer(name: str, website: str | None, phone: str | None) -> int:
    from bot.db.pg import fetchrow, fetchval, execute

    row = await fetchrow("SELECT id FROM developers WHERE lower(name) = lower($1)", name)
    if row:
        await execute("""
            UPDATE developers SET
                website     = COALESCE(website, $2),
                sales_phone = COALESCE(sales_phone, $3),
                updated_at  = now()
            WHERE id = $1
        """, row["id"], website, phone)
        return row["id"]
    return await fetchval("""
        INSERT INTO developers (name, website, sales_phone, aliases)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (name) DO UPDATE SET updated_at = now()
        RETURNING id
    """, name, website, phone, [name])


async def ensure_complex(source: str, dev_id: int, cx: ComplexData) -> int:
    """Upsert ЖК по нормализованному имени (единый матчинг с korter/homsters/
    krisha-complex-scan — один ЖК, много источников, см. complexes.source_info
    для тех обогащений и newbuild_source/_source_id для этого)."""
    from bot.db.pg import fetchrow, fetchval, execute, fetch

    deadlines = [u.deadline for u in cx.units if u.deadline]
    completion_year = min(deadlines).year if deadlines else None
    completion_quarter = _quarter(min(deadlines)) if deadlines else None
    # Комплекс уже сдан (год сдачи в прошлом) -> фактически вторичка, даже
    # если застройщик ещё продаёт остатки напрямую — пересчитываем каждый
    # прогон (не COALESCE), сам переключится с течением времени.
    current_year = datetime.now(timezone.utc).year
    is_newbuild = completion_year is None or completion_year >= current_year
    dm = _DISTRICT_RE.search(cx.address or "")
    district = dm.group(1).strip() if dm else None

    row = await fetchrow(
        "SELECT id, lat, lon, developer_id, address FROM complexes WHERE lower(trim(name)) = lower(trim($1))", cx.name)
    if row:
        cid = row["id"]
        # Entity resolution (фаза 1, docs/entity_resolution_plan.md): этот
        # источник только что нашёлся по точному совпадению имени с уже
        # существующим ЖК — записываем связь в spine (complex_source_links)
        # с confidence по сигналам имя+гео+застройщик+адрес, а не молча
        # теряем источник в одном из старых однослотовых полей ниже.
        # name_a=name_b=cx.name — совпадение уже гарантировано WHERE выше
        # (точный матч), пересчитывать через pg_trgm незачем.
        from bot.core.entity_resolution import score_match, record_source_link, ensure_complex_code
        conf, method = await score_match(
            cx.name, cx.name,
            existing_lat=row["lat"], existing_lon=row["lon"],
            candidate_lat=cx.lat, candidate_lon=cx.lon,
            developer_match=(row["developer_id"] is not None and row["developer_id"] == dev_id),
            existing_address=row["address"], candidate_address=cx.address,
        )
        await record_source_link(cid, source, cx.source_id, confidence=conf, method=method)
        await ensure_complex_code(cid)
        await execute("""
            UPDATE complexes SET
                developer_id        = COALESCE(developer_id, $2),
                district             = COALESCE(district, $3),
                address               = COALESCE(address, $4),
                housing_class          = COALESCE(housing_class, $5),
                is_newbuild             = $9,
                newbuild_source          = $10,
                newbuild_source_id        = $6,
                completion_year             = COALESCE($7, completion_year),
                completion_quarter            = COALESCE($8, completion_quarter),
                photo_url                       = COALESCE(photo_url, $11),
                photos                            = COALESCE(photos, $12),
                description                         = COALESCE(description, $13),
                newbuild_last_scan_at                 = now(),
                updated_at                              = now()
            WHERE id = $1
        """, cid, dev_id, district, cx.address, cx.housing_class, cx.source_id,
            completion_year, completion_quarter, is_newbuild, source,
            cx.photo_url, json.dumps([cx.photo_url]) if cx.photo_url else None, cx.description)
        if row["lat"] is None or row["lon"] is None:
            if cx.lat is not None and cx.lon is not None and in_astana_bbox(cx.lat, cx.lon):
                await execute("UPDATE complexes SET lat = $2, lon = $3 WHERE id = $1", cid, cx.lat, cx.lon)
            elif cx.address:
                await geocode_complex(cid, cx.address)
        return cid

    cid = await fetchval("""
        INSERT INTO complexes (name, developer_id, district, address, housing_class,
                               is_newbuild, newbuild_source, newbuild_source_id,
                               completion_year, completion_quarter, newbuild_last_scan_at,
                               photo_url, photos, description)
        VALUES ($1, $2, $3, $4, $5, $9, $10, $6, $7, $8, now(), $11, $12, $13)
        ON CONFLICT (lower(name)) DO UPDATE SET updated_at = now()
        RETURNING id
    """, cx.name, dev_id, district, cx.address, cx.housing_class, cx.source_id,
        completion_year, completion_quarter, is_newbuild, source,
        cx.photo_url, json.dumps([cx.photo_url]) if cx.photo_url else None, cx.description)
    # Этот источник — первый, кто принёс этот ЖК (новый entity_id) —
    # confidence максимальный, никакой неоднозначности нет (seed, а не match).
    from bot.core.entity_resolution import record_source_link, ensure_complex_code, score_match
    await record_source_link(cid, source, cx.source_id, confidence=1.0, method="seed_source")
    await ensure_complex_code(cid)

    # Fuzzy-проверка на дубль (задача ревью 2026-08-13): раз тут заводится
    # СОВСЕМ НОВЫЙ ЖК — не спутали ли его с уже существующим под чуть
    # другим именем (ребрендинг застройщиком, опечатка на одном из
    # сайтов)? Точного совпадения по имени быть не может (иначе попали бы
    # в ветку выше) — ищем похожие через pg_trgm и, если нашли, кладём
    # находку в очередь на подтверждение (record_source_link сам решит
    # review/conflict/skip по итоговому confidence) — НЕ трогаем cid,
    # только предлагаем на рассмотрение.
    try:
        near = await fetch("""
            SELECT id, name, lat, lon, developer_id, address
            FROM complexes WHERE id != $1 AND similarity(name, $2) >= 0.55
            ORDER BY similarity(name, $2) DESC LIMIT 3
        """, cid, cx.name)
        for n in near:
            conf2, method2 = await score_match(
                cx.name, n["name"],
                existing_lat=n["lat"], existing_lon=n["lon"],
                candidate_lat=cx.lat, candidate_lon=cx.lon,
                developer_match=(n["developer_id"] is not None and n["developer_id"] == dev_id),
                existing_address=n["address"], candidate_address=cx.address,
            )
            if conf2 >= 0.5:
                await record_source_link(n["id"], source, cx.source_id, confidence=conf2, method=method2)
    except Exception as e:
        log.warning("fuzzy-проверка на дубль ЖК %r не удалась: %s", cx.name, e)
    # Источник сам отдаёт точные координаты (NAK) -> используем их напрямую,
    # без Nominatim (тот всё равно менее точен, чем данные самого застройщика).
    # bbox-проверка (задача 2026-08-12, карантин координат) — источник тоже
    # может отдать битые данные, не доверяем вслепую.
    if cx.lat is not None and cx.lon is not None and in_astana_bbox(cx.lat, cx.lon):
        await execute("UPDATE complexes SET lat = $2, lon = $3 WHERE id = $1", cid, cx.lat, cx.lon)
    elif cx.address:
        await geocode_complex(cid, cx.address)
    return cid


async def save_complex(source: str, dev_id: int, cx: ComplexData, stats: dict) -> None:
    """Upsert всех юнитов ЖК + диф "пропал из выдачи -> sold" + история
    цены + пересчёт кэшированных счётчиков на complexes."""
    from bot.db.pg import fetch, execute

    if not cx.units:
        return
    complex_id = await ensure_complex(source, dev_id, cx)

    existing = await fetch(
        "SELECT id, source_unit_id, price, status FROM newbuild_units "
        "WHERE complex_id = $1 AND source = $2", complex_id, source)
    existing_by_id = {r["source_unit_id"]: r for r in existing}
    fresh_ids: set[str] = set()

    for u in cx.units:
        fresh_ids.add(u.source_unit_id)
        status = "available" if u.is_available else "reserved"
        prior = existing_by_id.get(u.source_unit_id)

        await execute("""
            INSERT INTO newbuild_units (
                complex_id, source, source_unit_id, developer_id, building, section,
                floor, floors_total, rooms, area, price, price_per_m2,
                layout_photo_url, photos, status, raw_json, updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,now())
            ON CONFLICT (source, source_unit_id) DO UPDATE SET
                building         = EXCLUDED.building,
                section           = EXCLUDED.section,
                floor              = EXCLUDED.floor,
                floors_total        = EXCLUDED.floors_total,
                rooms                 = EXCLUDED.rooms,
                area                   = EXCLUDED.area,
                price                   = EXCLUDED.price,
                price_per_m2             = EXCLUDED.price_per_m2,
                layout_photo_url          = EXCLUDED.layout_photo_url,
                photos                     = EXCLUDED.photos,
                status                      = EXCLUDED.status,
                developer_id                 = EXCLUDED.developer_id,
                raw_json                      = EXCLUDED.raw_json,
                last_seen_at                   = now(),
                sold_at                         = NULL,
                updated_at                       = now()
        """, complex_id, source, u.source_unit_id, dev_id, u.building, u.section,
            u.floor, u.floors_total, u.rooms, u.area, u.price, u.price_per_m2,
            u.layout_photo_url, json.dumps(u.photos) if u.photos else None, status,
            json.dumps(u.raw, ensure_ascii=False, default=str))

        if prior is None:
            stats["units_new"] = stats.get("units_new", 0) + 1
        else:
            stats["units_seen"] = stats.get("units_seen", 0) + 1
            if prior["price"] != u.price and u.price is not None:
                unit_row_id = prior["id"]
                await execute(
                    "INSERT INTO newbuild_unit_price_history (unit_id, price) VALUES ($1, $2)",
                    unit_row_id, u.price)
                stats["price_changes"] = stats.get("price_changes", 0) + 1

    # Пропавшие с прошлого прогона юниты этого ЖК -> считаем проданными.
    gone = [r for r in existing if r["source_unit_id"] not in fresh_ids and r["status"] != "sold"]
    for r in gone:
        await execute(
            "UPDATE newbuild_units SET status = 'sold', sold_at = now(), updated_at = now() "
            "WHERE id = $1", r["id"])
    stats["units_sold"] = stats.get("units_sold", 0) + len(gone)

    counts = await fetch(
        "SELECT status, COUNT(*) AS n FROM newbuild_units WHERE complex_id = $1 GROUP BY status",
        complex_id)
    active = sum(r["n"] for r in counts if r["status"] in ("available", "reserved"))
    sold = sum(r["n"] for r in counts if r["status"] == "sold")
    await execute(
        "UPDATE complexes SET newbuild_units_count = $2, newbuild_sold_count = $3 WHERE id = $1",
        complex_id, active, sold)

    stats["complexes"] = stats.get("complexes", 0) + 1
    log.info("  %s: активных=%d продано(всего)=%d новых=%d ушло_в_продажу=%d",
              cx.name, active, sold, len(fresh_ids - existing_by_id.keys()), len(gone))


async def run_source(source: str, developer_name: str, developer_website: str | None,
                      developer_phone: str | None, complexes: list[ComplexData]) -> dict:
    """Точка входа для конкретного сайта-источника: ensure_developer один раз,
    save_complex на каждый собранный ЖК, единая статистика."""
    dev_id = await ensure_developer(developer_name, developer_website, developer_phone)
    stats: dict = {"complexes": 0, "units_new": 0, "units_seen": 0, "units_sold": 0,
                   "price_changes": 0}
    for cx in complexes:
        try:
            await save_complex(source, dev_id, cx, stats)
        except Exception as e:
            log.exception("ЖК %s: ошибка записи: %s", cx.name, e)
    return stats
