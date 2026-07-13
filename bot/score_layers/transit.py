"""
Слой ТРАНСПОРТ: остановки общественного транспорта (OSM), 0..+3.
  3+ остановок в 400м → +3 (узел маршрутов)
  1-2 остановки в 400м → +2
  остановка в 700м     → +1
  ничего               →  0 (машинозависимая локация)
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
    stops_400 = sum(1 for p in poi if p["kind"] == "bus_stop" and p["dist_m"] <= 400)
    stops_700 = sum(1 for p in poi if p["kind"] == "bus_stop")
    if stops_400 >= 3:
        return 3, f"{stops_400} остановок в 400м — транспортный узел"
    if stops_400 >= 1:
        return 2, f"остановка в 400м"
    if stops_700 >= 1:
        return 1, "остановка в 700м"
    return 0, "остановок в 700м нет — только на машине"
