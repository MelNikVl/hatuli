"""
Общий POI-фетч для слоёв локации: ОДИН запрос Overpass (радиус 700 м)
вместо отдельного на каждый слой — бережём Overpass и ускоряем цикл.

Возвращает элементы с координатами; слои transit/amenities/parks
фильтруют их по своим радиусам через haversine.
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
    """Список [{kind, dist_m}] или None если OSM недоступен."""
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
        out.append({"kind": kind, "dist_m": haversine_m(lat, lon, coords[0], coords[1])})
    return out
