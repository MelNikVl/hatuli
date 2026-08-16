#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync_city_poi.py — Локальный OSM-слой (задача 2026-08-16, "Локальный
OSM-слой"): еженедельный плановый дамп POI Астаны из Overpass в city_poi,
чтобы критический путь скоринга (bot/score_layers/{schools,poi,noise}.py)
считал ВСЕГДА из PostgreSQL, без сети — Overpass остаётся только здесь,
в этом периодическом скрипте (city-poi-sync.timer, воскресенье 04:00), не
на каждом расчёте объявления. Найдено ровно на этом (медленно/недетерми-
нировано — 504/406/timeout зеркал) в test_university_only_no_poi_nearby,
задача "Исправь падение CI".

Предшественник — poi_import.py (корень репо, один общий запрос по
границе города, категории school/kindergarten/university/clinic/gov/
shop/park). Этот скрипт ЕГО НЕ ЗАМЕНЯЕТ файлово (poi_import.py остаётся
как есть, ручной инструмент) — но ПЕРЕЗАПИСЫВАЕТ те же kind при первом
прогоне (DELETE+INSERT по kind, см. save_category() ниже) более широким
набором категорий; после первого сравнения оба скрипта пишут ту же
семантику для school/kindergarten/university/shop/park, poi_import.py
для них фактически избыточен (не в скоупе — не удаляю, см. финальный
отчёт).

Категории (--category, группы из задачи "Локальный OSM-слой"):
    schools    — школы, детсады, вузы (amenity=school/kindergarten/university)
    transport  — остановки автобус/трамвай/метро
    health     — больницы, поликлиники, аптеки
    shops      — магазины, ТРЦ, кафе/рестораны (повседневный спрос)
    parks      — парки/скверы, спортобъекты
    negative   — промзоны, кладбища, свалки (НЕГАТИВНЫЕ факторы —
                 сознательно НЕ дают положительный вес в deal_score.py::
                 POI_WEIGHTS, см. его докстринг)
    roads      — крупные дороги (шум, bot/score_layers/noise.py) + ж/д
                 (данные про запас, потребителя пока нет)
    nightlife  — бары/ночные клубы (данные про запас, потребителя нет)

Каждая категория — kind (или несколько kind) в city_poi, ОДНА атомарная
транзакция DELETE FROM city_poi WHERE kind=ANY(...) + bulk INSERT — тот
же смысл, что TRUNCATE+INSERT (не оставляет "старых+новых вперемешку"),
но БЕЗ TRUNCATE ВСЕЙ таблицы: city_poi — общий справочник, в ней уже
живут kind='landmark' (ЛРТ-станции, hype_tracker/*.py) и kind, которыми
управляет poi_import.py — TRUNCATE стёр бы их тоже, DELETE по kind этой
категории — нет.

roads/railway — ways, не точки: чтобы искать "рядом с этой дорогой" без
PostGIS (в проекте её нет), каждый way сэмплируется точками через ~150м
вдоль geometry (см. _sample_polyline) — плотная точечная аппроксимация,
достаточная для радиусов 100/250м noise.py (см. его докстринг).

Идемпотентен: DELETE+INSERT по kind — второй прогон с теми же живыми
данными OSM даёт то же самое состояние (не растит дубли).

Запуск:
    venv/bin/python scripts/sync_city_poi.py --dry-run
    venv/bin/python scripts/sync_city_poi.py --dry-run --category schools
    venv/bin/python scripts/sync_city_poi.py            # реальная запись, ВСЕ категории
    venv/bin/python scripts/sync_city_poi.py --category roads
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

# scripts/ — не корень репо, тот же приём, что scripts/backfill_property_
# ids.py (задача 2026-08-16, "P1 — Property Identity").
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("sync_city_poi.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("sync_city_poi")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# Те же зеркала, что bot/score_layers/osm.py::OVERPASS_MIRRORS — не
# импортируем напрямую (тот модуль требует init_pool()-контекст выше по
# стеку для остального своего кода), но список должен оставаться синхро-
# низирован руками при правке одного из двух мест.
OVERPASS_MIRRORS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OVERPASS_TIMEOUT_S = 300.0  # бюджет ЖИВОГО зеркала на тяжёлый city-wide запрос
RETRY_DELAYS_S = [5, 15, 45]  # 3 попытки, экспоненциальный backoff

# НАХОДКА (сама эта задача, при первом реальном прогоне --dry-run): плоский
# httpx.AsyncClient(timeout=300.0) применяет 300с и к CONNECT, не только к
# чтению ответа — мёртвое/недоступное зеркало (в этом окружении обычно 2 из
# 4, см. bot/score_layers/osm.py) тогда виснет НА ВСЕ 300с ПЕРЕД тем, как
# вообще дойти до следующего, вместо мгновенного connection refused/route
# unreachable. На 4 зеркала x до 4 попыток это давало реалистичный worst
# case > часа на ОДНУ категорию. connect короткий (10с — дохлый маршрут
# фейлится быстро), read длинный (реальный бюджет тяжёлого запроса — тот
# живой мираж, который ответит, должен успеть).
_HTTPX_TIMEOUT = httpx.Timeout(connect=10.0, read=OVERPASS_TIMEOUT_S, write=30.0, pool=10.0)

_AREA = 'area["name"~"Астана|Nur-Sultan"]["boundary"="administrative"]["admin_level"~"2|4"]->.a;'

# ── Точечные категории: kind -> (Overpass-фильтры тегов, publicly-CLI категория) ──
# Каждый фильтр — тело внутри node(area.a)[...]/way(area.a)[...];
_POINT_KINDS: dict[str, str] = {
    "school": "[amenity=school]",
    "kindergarten": "[amenity=kindergarten]",
    "university": "[amenity=university]",
    "bus_stop": "[highway=bus_stop]",
    "shop": '[shop~"^(supermarket|convenience|greengrocer)$"]',
    "mall": "[shop=mall]",
    "food": '[amenity~"^(cafe|restaurant|fast_food)$"]',
    "pharmacy": "[amenity=pharmacy]",
    "clinic": "[amenity=clinic]",
    "hospital": "[amenity=hospital]",
    "park": '[leisure~"^(park|garden)$"]',
    "sports": '[leisure~"^(pitch|sports_centre|stadium|fitness_centre)$"]',
    "industrial": "[landuse=industrial]",
    "cemetery": "[landuse=cemetery]",
    "landfill": "[landuse=landfill]",
    "bar": '[amenity~"^(bar|nightclub|pub)$"]',
}
# ── Линейные категории (сэмплируются точками вдоль geometry) ──
_LINE_KINDS: dict[str, str] = {
    "road_major": '[highway~"^(motorway|trunk|primary)$"]',
    "road_secondary": "[highway=secondary]",
    "railway": "[railway=rail]",
}

# CLI --category -> список kind (публичные группы задачи "Локальный OSM-слой")
CATEGORIES: dict[str, list[str]] = {
    "schools": ["school", "kindergarten", "university"],
    "transport": ["bus_stop"],
    "health": ["hospital", "clinic", "pharmacy"],
    "shops": ["shop", "mall", "food"],
    "parks": ["park", "sports"],
    "negative": ["industrial", "cemetery", "landfill"],
    "roads": ["road_major", "road_secondary", "railway"],
    "nightlife": ["bar"],
}

_ROAD_SAMPLE_INTERVAL_M = 150.0


def _point_query(kind: str, timeout_s: int) -> str:
    tag_filter = _POINT_KINDS[kind]
    return f"""
[out:json][timeout:{timeout_s}];
{_AREA}
(
  node(area.a){tag_filter};
  way(area.a){tag_filter};
);
out tags center 20000;
"""


def _line_query(kind: str, timeout_s: int) -> str:
    tag_filter = _LINE_KINDS[kind]
    return f"""
[out:json][timeout:{timeout_s}];
{_AREA}
(
  way(area.a){tag_filter};
);
out geom 20000;
"""


async def _overpass_request(query: str) -> dict | None:
    """3 попытки с backoff [5s, 15s, 45s], внутри каждой — перебор
    зеркал (тот же принцип, что bot/score_layers/osm.py::
    overpass_request, но с retry поверх — плановый дамп может позволить
    себе подождать, в отличие от live-запроса на каждый listing)."""
    last_error = None
    for attempt, delay in enumerate([0] + RETRY_DELAYS_S, start=1):
        if delay:
            log.warning("retry %d/%d через %ds (последняя ошибка: %s)",
                        attempt - 1, len(RETRY_DELAYS_S), delay, last_error)
            await asyncio.sleep(delay)
        async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT) as client:
            for mirror in OVERPASS_MIRRORS:
                try:
                    resp = await client.post(mirror, data={"data": query})
                    if resp.status_code == 200:
                        return resp.json()
                    log.warning("overpass %s -> HTTP %s", mirror, resp.status_code)
                    last_error = f"HTTP {resp.status_code}"
                except Exception as exc:
                    log.warning("overpass %s failed: %s", mirror, exc)
                    last_error = str(exc)
    log.error("Overpass недоступен после %d попыток x %d зеркал. Последняя ошибка: %s",
               len(RETRY_DELAYS_S) + 1, len(OVERPASS_MIRRORS), last_error)
    return None


def _addr(tags: dict) -> str | None:
    street = tags.get("addr:street")
    house = tags.get("addr:housenumber")
    if street and house:
        return f"{street}, {house}"
    return street or None


def _extract_points(kind: str, data: dict) -> list[dict]:
    from bot.score_layers.osm import element_coords
    out, seen = [], set()
    for el in data.get("elements", []):
        coords = element_coords(el)
        if not coords or coords[0] is None:
            continue
        lat, lon = round(coords[0], 6), round(coords[1], 6)
        key = (lat, lon)
        if key in seen:
            continue
        seen.add(key)
        tags = el.get("tags") or {}
        out.append({
            "kind": kind, "lat": lat, "lon": lon,
            "name": tags.get("name") or tags.get("name:ru") or tags.get("name:kk"),
            "address": _addr(tags),
        })
    return out


def _sample_polyline(nodes: list[dict], interval_m: float) -> list[tuple[float, float]]:
    """Точки через ~interval_m вдоль polyline (нет PostGIS — плотная
    точечная аппроксимация линии, см. докстринг модуля). Первый узел
    всегда включён; дальше — как только накопленное расстояние с
    последней сэмплированной точки >= interval_m. Последний узел НЕ
    форсируется отдельно — на реальных длинах дорог разница в пределах
    interval_m на хвосте сегмента не имеет значения для радиусов
    100/250м noise.py."""
    from bot.score_layers.osm import haversine_m
    pts = [(n["lat"], n["lon"]) for n in nodes if n and n.get("lat") is not None and n.get("lon") is not None]
    if not pts:
        return []
    sampled = [pts[0]]
    acc = 0.0
    for i in range(1, len(pts)):
        acc += haversine_m(*pts[i - 1], *pts[i])
        if acc >= interval_m:
            sampled.append(pts[i])
            acc = 0.0
    return sampled


def _extract_line_points(kind: str, data: dict) -> list[dict]:
    out, seen = [], set()
    for el in data.get("elements", []):
        geometry = el.get("geometry") or []
        for lat, lon in _sample_polyline(geometry, _ROAD_SAMPLE_INTERVAL_M):
            key = (round(lat, 5), round(lon, 5))
            if key in seen:
                continue
            seen.add(key)
            out.append({"kind": kind, "lat": lat, "lon": lon, "name": None, "address": None})
    return out


async def fetch_kind(kind: str) -> list[dict] | None:
    """None — Overpass недоступен после всех попыток (вызывающий должен
    НЕ трогать существующие данные этой kind — см. sync())."""
    if kind in _POINT_KINDS:
        data = await _overpass_request(_point_query(kind, int(OVERPASS_TIMEOUT_S) - 10))
        if data is None:
            return None
        return _extract_points(kind, data)
    data = await _overpass_request(_line_query(kind, int(OVERPASS_TIMEOUT_S) - 10))
    if data is None:
        return None
    return _extract_line_points(kind, data)


async def save_category(kind: str, points: list[dict]) -> int:
    """DELETE FROM city_poi WHERE kind=$1 + bulk INSERT — ОДНА транзакция
    (см. докстринг модуля про "не TRUNCATE всей таблицы, но атомарно для
    ЭТОЙ kind"). Возвращает число записанных строк."""
    from bot.db.pg import get_pool
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM city_poi WHERE kind = $1", kind)
            if points:
                await conn.executemany(
                    """INSERT INTO city_poi (kind, name, lat, lon, address, updated_at)
                       VALUES ($1, $2, $3, $4, $5, now())
                       ON CONFLICT (kind, lat, lon) DO UPDATE
                         SET name = EXCLUDED.name, address = EXCLUDED.address,
                             updated_at = now()""",
                    [(p["kind"], p["name"], p["lat"], p["lon"], p["address"]) for p in points],
                )
    return len(points)


async def run_sync(dry_run: bool = False, category: str | None = None) -> dict:
    """Возвращает {kind: count} по фактически обработанным kind (и
    {"_failed": [kind, ...]} — категории, для которых Overpass не ответил
    ни разу после всех попыток; их city_poi НЕ трогаем — старые данные
    остаются, лучше устаревшие, чем пустые, freshness-деградация confidence
    — см. bot/core/location_score.py::_apply_freshness_confidence_penalty)."""
    kinds = CATEGORIES[category] if category else [k for cat in CATEGORIES.values() for k in cat]
    stats: dict[str, int] = {}
    failed: list[str] = []
    for kind in kinds:
        log.info("категория %s: запрос к Overpass...", kind)
        points = await fetch_kind(kind)
        if points is None:
            failed.append(kind)
            log.error("категория %s: Overpass недоступен, city_poi для неё НЕ тронута", kind)
            continue
        if dry_run:
            stats[kind] = len(points)
            log.info("категория %s: %d POI (--dry-run, не записано)", kind, len(points))
            continue
        n = await save_category(kind, points)
        stats[kind] = n
        log.info("категория %s: записано %d POI", kind, n)
    if failed:
        stats["_failed"] = failed
    return stats


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="ничего не писать, только сводка")
    ap.add_argument("--category", choices=sorted(CATEGORIES), default=None,
                     help="обновить только одну категорию (по умолчанию — все)")
    args = ap.parse_args()

    if args.dry_run:
        stats = await run_sync(dry_run=True, category=args.category)
        log.info("ИТОГ (DRY-RUN, ничего не записано): %s", stats)
        print(stats)
        return

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        stats = await run_sync(dry_run=False, category=args.category)
        log.info("ИТОГ (запись выполнена): %s", stats)
        print(stats)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
