"""
Слой ПАРКИ: зелёные зоны в пешей доступности (OSM), 0..+2.
  парк <400м → +2
  парк <700м → +1
"""
from __future__ import annotations
from bot.score_layers.poi import fetch_poi


async def compute(listing: dict) -> tuple[int, str]:
    lat, lon = listing.get("lat"), listing.get("lon")
    if not lat or not lon:
        return 0, "нет координат"
    poi = await fetch_poi(lat, lon)
    if poi is None:
        return 0, "OSM недоступен"
    parks = [p["dist_m"] for p in poi if p["kind"] == "park"]
    if not parks:
        return 0, "парков в 700м нет"
    d = min(parks)
    if d <= 400:
        return 2, f"парк в {d:.0f}м"
    return 1, f"парк в {d:.0f}м"
