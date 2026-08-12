"""
Geo utilities: Nominatim geocoding + haversine distance.

No API key required. Uses the public Nominatim endpoint (OpenStreetMap).
Rate limit: 1 request/second — respected via asyncio.sleep(1.1).
"""
from __future__ import annotations

import asyncio
import logging
import math

import httpx

logger = logging.getLogger(__name__)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_HEADERS = {
    "User-Agent": "KrishaTelegramBot/2.0 (real-estate search; contact via github)",
    "Accept-Language": "ru,en",
}

# Астана + разумный запас (растущие районы, соседние посёлки), НЕ вся
# область (задача 2026-08-12, карантин координат: geo_quarantine.py
# нашёл значения в 899 км/475 км от остальных точек того же ЖК —
# Туркестан/Караганда вместо Астаны — ни один парсер геоданных их не
# отсеивал на входе, дошли до complexes.lat/lon и homeportal_objects
# невалидированными). Использовать ПЕРЕД записью lat/lon в БД в любом
# парсере, который сам вычисляет/парсит координаты — грубая, дешёвая
# защита, не замена геокодингу/точности.
ASTANA_BBOX = (50.70, 51.55, 70.80, 72.10)  # lat_min, lat_max, lon_min, lon_max


def in_astana_bbox(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    lat_min, lat_max, lon_min, lon_max = ASTANA_BBOX
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max

# Module-level lock: ensures only one geocoding request runs at a time
_geocode_lock = asyncio.Lock()
_last_request_time: float = 0.0


async def geocode(address: str, city: str | None = None) -> tuple[float, float] | None:
    """
    Geocode an address string to (lat, lon) via Nominatim.

    Appends city + "Казахстан" to narrow down results.
    Returns None if the address cannot be resolved.

    Rate-limited to 1 request/sec (Nominatim ToS).
    """
    global _last_request_time

    parts = [address]
    if city:
        city_label = {"astana": "Астана", "almaty": "Алматы"}.get(city.lower(), city)
        parts.append(city_label)
    parts.append("Казахстан")
    query = ", ".join(parts)

    async with _geocode_lock:
        # Enforce 1.1s gap between requests
        import time
        elapsed = time.monotonic() - _last_request_time
        if elapsed < 1.1:
            await asyncio.sleep(1.1 - elapsed)

        try:
            async with httpx.AsyncClient(headers=_HEADERS, timeout=10.0) as client:
                resp = await client.get(
                    _NOMINATIM_URL,
                    params={"q": query, "format": "json", "limit": "1"},
                )
                _last_request_time = time.monotonic()
                resp.raise_for_status()
                results = resp.json()

            if not results:
                logger.debug("Nominatim: no results for %r", query)
                return None

            lat = float(results[0]["lat"])
            lon = float(results[0]["lon"])
            # bbox-защита (см. ASTANA_BBOX выше) — только когда явно
            # искали в Астане/без указания города (по умолчанию), иначе
            # легитимный результат по другому городу ложно отбросим.
            if (city is None or city.lower() == "astana") and not in_astana_bbox(lat, lon):
                logger.warning("Nominatim: %r → (%.5f, %.5f) вне bbox Астаны, отбрасываю", query, lat, lon)
                return None
            logger.debug("Nominatim: %r → (%.5f, %.5f)", query, lat, lon)
            return lat, lon

        except Exception as exc:
            logger.warning("Nominatim geocoding failed for %r: %s", query, exc)
            return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Return the great-circle distance in kilometres between two points.
    Uses the Haversine formula.
    """
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def within_radius(
    user_lat: float, user_lon: float, radius_km: float,
    listing_lat: float, listing_lon: float,
) -> bool:
    """Return True if the listing is within radius_km of the user's point."""
    return haversine_km(user_lat, user_lon, listing_lat, listing_lon) <= radius_km
