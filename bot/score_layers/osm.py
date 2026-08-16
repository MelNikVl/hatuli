"""
Общий помощник слоёв: OSM Overpass с кешем в PostgreSQL.

Координаты округляются до 3 знаков (~110 м сетка) — один запрос к Overpass
на ячейку, дальше ответ берётся из кеша (osm_cache) 60 дней.
Overpass бесплатный, но просит вежливости: не дёргаем чаще необходимого.

НАДЁЖНОСТЬ: у Overpass несколько независимых серверов-зеркал с одинаковым
API. Основной overpass-api.de иногда недоступен (перегрузка/бан IP/сетевые
проблемы конкретного хостера) — пробуем по очереди несколько зеркал, чтобы
единая точка отказа не гасила все 5 слоёв локации разом.
"""
from __future__ import annotations

import json
import logging

import httpx

from bot.db.pg import execute, fetchrow

logger = logging.getLogger(__name__)

# Порядок = порядок попыток. Все зеркала совместимы по API.
OVERPASS_MIRRORS = [
    # maps.mail.ru — первым: единственное зеркало, доступное с сервера
    # (проверено 14.07.2026: остальные — connection refused либо таймаут,
    # похоже на блокировку маршрутов до европейских хостеров). Остальные
    # оставлены как фолбэк на случай, если доступность изменится.
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
CACHE_DAYS = 60


def grid(v: float) -> float:
    return round(v, 3)


async def overpass_request(query: str, timeout: float = 30.0) -> dict | None:
    """POST-запрос к Overpass с перебором зеркал при отказе."""
    last_error = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for mirror in OVERPASS_MIRRORS:
            try:
                resp = await client.post(mirror, data={"data": query})
                if resp.status_code == 200:
                    return resp.json()
                logger.warning("overpass %s -> HTTP %s", mirror, resp.status_code)
                last_error = f"HTTP {resp.status_code}"
            except Exception as exc:
                logger.warning("overpass %s failed: %s", mirror, exc)
                last_error = str(exc)
    logger.error("Все зеркала Overpass недоступны. Последняя ошибка: %s. "
                "Проверь сеть: curl -v %s", last_error, OVERPASS_MIRRORS[0])
    return None


async def overpass_cached(lat: float, lon: float, kind: str, query: str) -> dict | None:
    """Запрос к Overpass с кешем. query — готовый Overpass QL."""
    glat, glon = grid(lat), grid(lon)
    try:
        row = await fetchrow(
            "SELECT payload FROM osm_cache WHERE grid_lat=$1 AND grid_lon=$2 AND kind=$3 "
            "AND fetched_at > now() - ($4 || ' days')::interval",
            glat, glon, kind, str(CACHE_DAYS),
        )
        if row:
            payload = row["payload"]
            return json.loads(payload) if isinstance(payload, str) else payload
    except Exception as exc:
        logger.warning("osm_cache read failed: %s", exc)

    data = await overpass_request(query)
    if data is None:
        return None

    try:
        await execute(
            """INSERT INTO osm_cache (grid_lat, grid_lon, kind, payload, fetched_at)
               VALUES ($1,$2,$3,$4::jsonb, now())
               ON CONFLICT (grid_lat, grid_lon, kind)
               DO UPDATE SET payload=$4::jsonb, fetched_at=now()""",
            glat, glon, kind, json.dumps(data),
        )
    except Exception as exc:
        logger.warning("osm_cache write failed: %s", exc)
    return data


# Минимальный дешёвый запрос для healthcheck — один конкретный узел по id,
# не гео-запрос: проверяем, отвечает ли зеркало вообще, не грузим его
# реальными данными (Фаза L1, docs/location_product_design.md §7,
# задача 2026-08-14, п.9 "Часть 3" scoring_roadmap.md — "healthcheck
# зеркал Overpass с алертом при <2 живых").
_HEALTHCHECK_QUERY = "[out:json][timeout:10];node(1);out;"


async def check_mirrors(timeout: float = 10.0) -> dict[str, bool]:
    """Проверяет КАЖДОЕ зеркало из OVERPASS_MIRRORS независимо (в отличие
    от overpass_request(), который перебирает по очереди и останавливается
    на первом живом) — возвращает {mirror_url: alive}. Используется
    osm_mirrors_healthcheck.py, не участвует в обычном пути слоёв
    локации (тот по-прежнему через overpass_request/overpass_cached)."""
    result: dict[str, bool] = {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for mirror in OVERPASS_MIRRORS:
            try:
                resp = await client.post(mirror, data={"data": _HEALTHCHECK_QUERY})
                result[mirror] = resp.status_code == 200
                if resp.status_code != 200:
                    logger.warning("healthcheck %s -> HTTP %s", mirror, resp.status_code)
            except Exception as exc:
                logger.warning("healthcheck %s failed: %s", mirror, exc)
                result[mirror] = False
    return result


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между точками в метрах."""
    import math
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def element_coords(el: dict) -> tuple[float, float] | None:
    """Координаты элемента Overpass (node → lat/lon, way → center)."""
    if "lat" in el and "lon" in el:
        return el["lat"], el["lon"]
    c = el.get("center")
    if c:
        return c.get("lat"), c.get("lon")
    return None


# ── Локальный city_poi вместо живого Overpass (задача 2026-08-16,
# "Локальный OSM-слой" — план еженедельного дампа scripts/sync_city_poi.py,
# см. его докстринг) ──────────────────────────────────────────────────────
#
# Критический путь скоринга больше не должен ходить в Overpass на каждый
# расчёт (медленно, недетерминированно — 504/406/timeout зеркал поймали
# ровно на этом в test_university_only_no_poi_nearby, задача "Исправь
# падение CI"). city_poi наполняется РАЗ В НЕДЕЛЮ (city-poi-sync.timer),
# слои читают её синхронно (без сети) и падают на overpass_cached()
# ТОЛЬКО если конкретная категория ещё ни разу не синхронизировалась
# (см. kinds_synced() ниже — критично: проверка ИМЕННО по нужным kind,
# не "city_poi вообще не пуста" — после этой задачи в таблице параллельно
# живут десятки kind, «непустая» больше не значит «нужная категория
# наполнена»; ровно этот баг чуть не унаследовал bot/score_layers/
# schools.py::_from_local_table — было "SELECT COUNT(*) FROM city_poi"
# без WHERE kind, здесь и там исправлено на per-kind счётчик).
_M_PER_DEG_LAT = 111_000.0
# Астана ~51° с.ш., cos(51°)≈0.63 — тот же коэффициент долгота->метры,
# что уже используется в bot/score_layers/parks.py::_polygon_area_m2 и
# в SQL-запросах bot/core/location_score.py.
_LON_SCALE = 0.63


def _bbox_margin_deg(radius_m: float) -> tuple[float, float]:
    """(lat_margin, lon_margin) в градусах — bbox ЗАВЕДОМО шире radius_m
    (+15% запас на грубость LON_SCALE), точная отсечка — haversine_m по
    кандидатам bbox, тот же двухшаговый приём, что в schools.py::
    _from_local_table (bbox дёшево через индекс idx_city_poi_geo, точное
    расстояние — уже не по индексу, но кандидатов после bbox мало)."""
    lat_m = radius_m / _M_PER_DEG_LAT * 1.15
    lon_m = radius_m / (_M_PER_DEG_LAT * _LON_SCALE) * 1.15
    return lat_m, lon_m


async def kinds_synced(kinds: list[str]) -> bool:
    """Хотя бы одна запись city_poi с kind ИЗ ЭТОГО набора — то есть эта
    КОНКРЕТНАЯ категория хоть раз прошла sync_city_poi.py (а не "таблица
    вообще не пуста", см. докстринг выше). False -> вызывающий слой честно
    падает на overpass_cached() (см. каждый local_* ниже)."""
    from bot.db.pg import fetchval
    try:
        return bool(await fetchval(
            "SELECT EXISTS(SELECT 1 FROM city_poi WHERE kind = ANY($1::text[]))", kinds))
    except Exception as exc:
        logger.warning("city_poi kinds_synced(%s) failed: %s", kinds, exc)
        return False


async def local_poi_near(lat: float, lon: float, kinds: list[str],
                          radius_m: float) -> list[dict] | None:
    """city_poi-эквивалент Overpass around:radius_m — [{kind, lat, lon}]
    в радиусе radius_m от точки, только среди kinds. None — ни одна из
    kinds ещё не синхронизирована (кандидат на overpass_cached()-фолбэк);
    [] — синхронизирована, но реально ничего рядом (валидный, не
    "ошибочный", результат — Unknown ≠ average: пусто и "не знаем" это
    РАЗНЫЕ вещи, поэтому None и [] не путаются)."""
    if not await kinds_synced(kinds):
        return None
    from bot.db.pg import fetch
    lat_m, lon_m = _bbox_margin_deg(radius_m)
    try:
        # ::double precision явно — тот же AmbiguousFunctionError, что уже
        # ловили в bot/identity/property_linker.py::_find_fuzzy_candidate
        # (asyncpg не может вывести тип "$2 - $3" без якоря в выражении).
        rows = await fetch(
            """
            SELECT kind, lat, lon FROM city_poi
            WHERE kind = ANY($1::text[])
              AND lat BETWEEN $2::double precision - $3::double precision
                          AND $2::double precision + $3::double precision
              AND lon BETWEEN $4::double precision - $5::double precision
                          AND $4::double precision + $5::double precision
            """,
            kinds, lat, lat_m, lon, lon_m,
        )
    except Exception as exc:
        logger.warning("city_poi local_poi_near failed: %s", exc)
        return None
    return [{"kind": r["kind"], "lat": r["lat"], "lon": r["lon"]}
            for r in rows if haversine_m(lat, lon, r["lat"], r["lon"]) <= radius_m]


async def city_poi_freshness_days(kinds: list[str]) -> float | None:
    """Возраст в днях САМОЙ СТАРОЙ записи city_poi среди kinds (не
    средней/самой свежей — самое старое звено определяет реальный риск
    "давно не обновлялось"). None — категория ещё ни разу не
    синхронизировалась (нечего "устаревать" — это другой кейс, см.
    bot/core/location_score.py::_apply_freshness_confidence_penalty)."""
    from bot.db.pg import fetchval
    from datetime import datetime, timezone
    try:
        oldest = await fetchval(
            "SELECT MIN(updated_at) FROM city_poi WHERE kind = ANY($1::text[])", kinds)
    except Exception as exc:
        logger.warning("city_poi_freshness_days(%s) failed: %s", kinds, exc)
        return None
    if oldest is None:
        return None
    return (datetime.now(timezone.utc) - oldest).total_seconds() / 86400.0
