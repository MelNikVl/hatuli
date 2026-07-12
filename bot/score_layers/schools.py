"""
Слой ШКОЛЫ/САДИКИ/ВУЗЫ: пешая доступность по OSM (радиус 700 м).

  школа + садик в 700 м → +5
  школа или садик       → +3
  только вуз            → +2 (важно для аренды студентам)
  ничего                →  0

Рейтинг заведений — следующий шаг (отдельный источник данных),
пока считаем сам факт доступности.
"""
from __future__ import annotations

from bot.score_layers.osm import overpass_cached

_QUERY = """
[out:json][timeout:20];
(
  node(around:700,{lat},{lon})[amenity=school];
  way(around:700,{lat},{lon})[amenity=school];
  node(around:700,{lat},{lon})[amenity=kindergarten];
  way(around:700,{lat},{lon})[amenity=kindergarten];
  node(around:700,{lat},{lon})[amenity=university];
  way(around:700,{lat},{lon})[amenity=university];
);
out tags center 50;
"""


async def compute(listing: dict) -> tuple[int, str]:
    lat, lon = listing.get("lat"), listing.get("lon")
    if not lat or not lon:
        return 0, "нет координат"

    data = await overpass_cached(lat, lon, "schools",
                                 _QUERY.format(lat=lat, lon=lon))
    if data is None:
        return 0, "OSM недоступен"

    kinds = set()
    for el in data.get("elements", []):
        am = (el.get("tags") or {}).get("amenity")
        if am:
            kinds.add(am)

    has_school = "school" in kinds
    has_kg = "kindergarten" in kinds
    has_uni = "university" in kinds

    if has_school and has_kg:
        return 5, "школа и садик в 700м — семейная локация"
    if has_school or has_kg:
        what = "школа" if has_school else "садик"
        return 3, f"{what} в 700м"
    if has_uni:
        return 2, "вуз рядом — арендный спрос студентов"
    return 0, "школ/садиков в 700м не найдено"
