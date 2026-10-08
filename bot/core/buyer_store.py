"""Buyer MVP persistence: existing PostgreSQL users and favorites only."""
from __future__ import annotations

import json
from bot.db import pg
from bot.core.site_auth import add_favorite
from bot.core.geo import in_astana_bbox


async def ensure_user(user_id: int, username: str | None = None) -> None:
    # Do not overwrite site profile, notification consent or existing filters.
    await pg.execute("""INSERT INTO users (user_id, username, notify_frequency)
        VALUES ($1, $2, 'off') ON CONFLICT (user_id) DO NOTHING""", user_id, username)


async def get_profile(user_id: int) -> dict:
    row = await pg.fetchrow("""SELECT budget_max, rooms, area_min, property_type, location_lat, location_lon, radius_km, buyer_map
        FROM users WHERE user_id=$1""", user_id)
    profile = dict(row) if row else {}
    map_data = profile.pop('buyer_map', None) or {}
    if isinstance(map_data, str):
        map_data = json.loads(map_data)
    if map_data.get('selection'):
        profile['buyer_area'] = map_data['selection']
    rooms = profile.get('rooms') or []
    if isinstance(rooms, str):
        try:
            rooms = json.loads(rooms)
        except ValueError:
            rooms = rooms.split(',')
    if not isinstance(rooms, list):
        rooms = [rooms]
    profile['rooms'] = [int(str(r).rstrip('+')) for r in rooms
                        if str(r).rstrip('+').isdigit()]
    if any(profile.get(k) is None for k in ('location_lat', 'location_lon', 'radius_km')):
        for k in ('location_lat', 'location_lon', 'radius_km'):
            profile.pop(k, None)
    return profile


def validate_profile(profile: dict) -> dict:
    budget = int(profile['budget_max'])
    area = float(profile.get('area_min') or 0)
    rooms = sorted(set(int(r) for r in profile['rooms']))
    kind = profile.get('property_type')
    if not 1_000_000 <= budget <= 2_000_000_000:
        raise ValueError('Бюджет должен быть от 1 до 2000 млн ₸.')
    if not 0 <= area <= 1000 or not rooms or any(r not in (1, 2, 3, 4) for r in rooms):
        raise ValueError('Проверьте площадь и комнатность.')
    if kind not in (None, 'new', 'secondary'):
        raise ValueError('Неизвестный тип рынка.')
    result = dict(budget_max=budget, area_min=area or None, rooms=rooms, property_type=kind)
    if any(k in profile for k in ('location_lat', 'location_lon', 'radius_km')):
        try:
            lat, lon = float(profile['location_lat']), float(profile['location_lon'])
            radius = profile['radius_km']
        except (KeyError, TypeError, ValueError):
            raise ValueError('Выберите точку на карте и радиус.') from None
        if not in_astana_bbox(lat, lon) or radius not in (1, 2, 3, 5, 10):
            raise ValueError('Выберите точку в Астане и доступный радиус.')
        result.update(location_lat=lat, location_lon=lon, radius_km=int(radius))
    return result


async def save_profile(user_id: int, profile: dict, *, execute=None) -> None:
    p = validate_profile(profile)
    execute = execute or pg.execute
    await execute("""INSERT INTO users
        (user_id, budget_max, rooms, area_min, property_type, location_lat, location_lon, radius_km, notify_frequency)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'off') ON CONFLICT (user_id) DO UPDATE SET
        budget_max=EXCLUDED.budget_max, rooms=EXCLUDED.rooms,
        area_min=EXCLUDED.area_min, property_type=EXCLUDED.property_type,
        location_lat=CASE WHEN $9 THEN EXCLUDED.location_lat ELSE users.location_lat END,
        location_lon=CASE WHEN $9 THEN EXCLUDED.location_lon ELSE users.location_lon END,
        radius_km=CASE WHEN $9 THEN EXCLUDED.radius_km ELSE users.radius_km END,
        buyer_map=CASE WHEN $9 THEN users.buyer_map-'selection'-'draft' ELSE users.buyer_map END, updated_at=now()""",
        user_id, p['budget_max'], json.dumps(p['rooms']), p['area_min'], p['property_type'],
        p.get('location_lat'), p.get('location_lon'), p.get('radius_km'), 'location_lat' in p)


async def save_favorite(user_id: int, listing_id: str) -> bool:
    if not await pg.fetchrow('SELECT id FROM apartment_listings WHERE id=$1', listing_id):
        return False
    await ensure_user(user_id)
    await add_favorite(user_id, listing_id)
    return True
