"""
Слой ШУМ (внешний): близость крупных дорог по OSM.

Логика: ищем в радиусе 250 м дороги классов motorway/trunk/primary/secondary.
  magistral (motorway/trunk/primary) в <100 м  → −6
  magistral в <250 м                            → −4
  secondary в <100 м                            → −3
  secondary в <250 м                            → −1
  ничего крупного рядом                         →  0 (тихо — это норма, не бонус)

Внутренний шум (звукоизоляция) по данным не определить — он остаётся
в вопросах продавцу и в AI-анализе отзывов (позже).
"""
from __future__ import annotations

from bot.score_layers.osm import overpass_cached

_QUERY = """
[out:json][timeout:20];
(
  way(around:250,{lat},{lon})[highway~"^(motorway|trunk|primary)$"];
  way(around:250,{lat},{lon})[highway=secondary];
);
out tags center 30;
"""

_QUERY_NEAR = """
[out:json][timeout:20];
(
  way(around:100,{lat},{lon})[highway~"^(motorway|trunk|primary)$"];
  way(around:100,{lat},{lon})[highway=secondary];
);
out tags center 30;
"""


def _classify(elements: list) -> tuple[bool, bool]:
    """(есть_магистраль, есть_secondary)"""
    has_major = has_secondary = False
    for el in elements:
        hw = (el.get("tags") or {}).get("highway", "")
        if hw in ("motorway", "trunk", "primary"):
            has_major = True
        elif hw == "secondary":
            has_secondary = True
    return has_major, has_secondary


async def compute(listing: dict) -> tuple[int, str]:
    lat, lon = listing.get("lat"), listing.get("lon")
    if not lat or not lon:
        return 0, "нет координат"

    far = await overpass_cached(lat, lon, "roads250",
                                _QUERY.format(lat=lat, lon=lon))
    if far is None:
        return 0, "OSM недоступен"
    major_250, sec_250 = _classify(far.get("elements", []))
    if not major_250 and not sec_250:
        return 0, "крупных дорог в 250м нет — тихо"

    near = await overpass_cached(lat, lon, "roads100",
                                 _QUERY_NEAR.format(lat=lat, lon=lon))
    major_100, sec_100 = _classify((near or {}).get("elements", []))

    if major_100:
        return -6, "магистраль в <100м — шумно"
    if major_250:
        return -4, "магистраль в <250м"
    if sec_100:
        return -3, "оживлённая улица в <100м"
    return -1, "оживлённая улица в <250м"
