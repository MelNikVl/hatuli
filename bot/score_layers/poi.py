"""
Общий POI-фетч для слоёв локации: ОДИН запрос Overpass (радиус 700 м)
вместо отдельного на каждый слой — бережём Overpass и ускоряем цикл.

Возвращает элементы с координатами; слои transit/amenities/parks
фильтруют их по своим радиусам через haversine.

lat/lon в каждом элементе (Фаза L2, docs/location_product_design.md,
задача 2026-08-14) — добавлены для карты со слоями на /complex/{id}
(bot/core/complex_location_detail.py); transit.py/amenities.py/parks.py
как использовали только kind/dist_m, так и продолжают — обратная
совместимость не нарушена, поля просто раньше не читались.

id/type (задача 2026-08-15, "детализация parks.py" — площадь ближайшего
парка в га) — тот же приём: id/type у элемента Overpass есть ВСЕГДА,
независимо от verbosity `out`-запроса (не только у geometry-запросов),
добавление этих двух полей ничего не меняет для существующих читателей.
parks.py использует их, чтобы точечно (по id) дозапросить геометрию
ИМЕННО ближайшего парка — не всех парков в радиусе, чтобы не раздувать
этот общий 700м-запрос ради данных, нужных только одному слою.
"""
from __future__ import annotations

from bot.score_layers.osm import overpass_cached, haversine_m, element_coords

_QUERY = """
[out:json][timeout:25];
(
  node(around:700,{lat},{lon})[highway=bus_stop];
  node(around:700,{lat},{lon})[shop~"^(supermarket|convenience|greengrocer|chemist)$"];
  way(around:700,{lat},{lon})[shop=supermarket];
  node(around:700,{lat},{lon})[amenity~"^(pharmacy|cafe|restaurant|fast_food|clinic|marketplace|bank)$"];
  way(around:700,{lat},{lon})[leisure~"^(park|garden)$"];
  node(around:700,{lat},{lon})[leisure=park];
);
out tags center 120;
"""


async def fetch_poi(lat: float, lon: float) -> list[dict] | None:
    """Список [{kind, dist_m, lat, lon, id, type}] или None если OSM
    недоступен."""
    data = await overpass_cached(lat, lon, "poi700", _QUERY.format(lat=lat, lon=lon))
    if data is None:
        return None
    out = []
    for el in data.get("elements", []):
        coords = element_coords(el)
        if not coords or coords[0] is None:
            continue
        tags = el.get("tags") or {}
        if tags.get("highway") == "bus_stop":
            kind = "bus_stop"
        elif tags.get("shop"):
            kind = "shop"
        elif tags.get("amenity") in ("pharmacy", "clinic"):
            kind = "health"
        elif tags.get("amenity") in ("cafe", "restaurant", "fast_food"):
            kind = "food"
        elif tags.get("amenity") in ("marketplace", "bank"):
            kind = "service"
        elif tags.get("leisure") in ("park", "garden"):
            kind = "park"
        else:
            continue
        out.append({"kind": kind, "dist_m": haversine_m(lat, lon, coords[0], coords[1]),
                     "lat": coords[0], "lon": coords[1],
                     "id": el.get("id"), "type": el.get("type")})
    return out
