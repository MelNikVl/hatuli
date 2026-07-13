"""
Зоны приоритета: полигоны, нарисованные мышкой на карте (/admin/zones).

Объявление, попавшее внутрь зоны, получает бонус к скору (zone_bonus).
Проверка точка-в-полигоне — ray casting, без внешних зависимостей.

Использование в цикле парсера:
    from bot.core.zones import load_zones, zone_bonus_for
    zones = await load_zones()
    bonus, zone_name = zone_bonus_for(lat, lon, zones)
"""
from __future__ import annotations

import json
import logging

from bot.db.pg import fetch

logger = logging.getLogger(__name__)


async def load_zones() -> list[dict]:
    """Загрузить все зоны из БД. Возвращает [] если таблицы нет."""
    try:
        rows = await fetch("SELECT id, name, bonus, polygon FROM priority_zones")
    except Exception as exc:
        logger.warning("load_zones failed: %s", exc)
        return []
    zones = []
    for r in rows:
        poly = r["polygon"]
        if isinstance(poly, str):
            poly = json.loads(poly)
        # poly: [[lon,lat], [lon,lat], ...] (GeoJSON ring)
        if poly and isinstance(poly[0][0], list):  # вложенный ring [[[..]]]
            poly = poly[0]
        zones.append({"id": r["id"], "name": r["name"], "bonus": r["bonus"], "ring": poly})
    return zones


def _point_in_ring(lat: float, lon: float, ring: list) -> bool:
    """Ray casting. ring = [[lon, lat], ...]."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]   # lon, lat
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def zone_bonus_for(lat: float | None, lon: float | None,
                   zones: list[dict]) -> tuple[int, str | None]:
    """
    Бонус зоны для точки. Зоны могут быть и ПОЛОЖИТЕЛЬНЫМИ (приоритет),
    и ОТРИЦАТЕЛЬНЫМИ (анти-зоны: промзона, окраина...). Если точка попала
    в несколько зон — берётся самая "сильная" по модулю (сильный минус
    перевешивает слабый плюс и наоборот).
    """
    if not lat or not lon or not zones:
        return 0, None
    best_bonus, best_name = 0, None
    for z in zones:
        try:
            if _point_in_ring(lat, lon, z["ring"]) and abs(z["bonus"]) > abs(best_bonus):
                best_bonus, best_name = z["bonus"], z["name"]
        except Exception:
            continue
    return best_bonus, best_name
