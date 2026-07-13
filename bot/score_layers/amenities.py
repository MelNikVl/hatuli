"""
Слой СЕРВИСЫ (walkability): магазины, аптеки, кафе в 500м (OSM), 0..+4.
Идея Walk Score: количество и разнообразие точек повседневного спроса.
  супермаркет <300м → базовый +2 (главный маркер удобства)
  разнообразие категорий (shop/health/food/service) в 500м добавляет
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
    near = [p for p in poi if p["dist_m"] <= 500 and p["kind"] in ("shop", "health", "food", "service")]
    kinds = {p["kind"] for p in near}
    has_market_300 = any(p["kind"] == "shop" and p["dist_m"] <= 300 for p in near)

    score = 0
    parts = []
    if has_market_300:
        score += 2
        parts.append("магазин в 300м")
    elif "shop" in kinds:
        score += 1
        parts.append("магазин в 500м")
    if "health" in kinds:
        score += 1
        parts.append("аптека/клиника")
    if "food" in kinds and len([p for p in near if p["kind"] == "food"]) >= 2:
        score += 1
        parts.append("кафе рядом")
    score = min(score, 4)
    return score, ("пешая доступность: " + ", ".join(parts)) if parts else "сервисов в 500м не найдено"
