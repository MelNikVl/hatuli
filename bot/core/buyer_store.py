"""Buyer MVP persistence: existing PostgreSQL users and favorites only."""
from __future__ import annotations

import json
from bot.db import pg
from bot.core.site_auth import add_favorite


async def ensure_user(user_id: int, username: str | None = None) -> None:
    # Do not overwrite site profile, notification consent or existing filters.
    await pg.execute("""INSERT INTO users (user_id, username, notify_frequency)
        VALUES ($1, $2, 'off') ON CONFLICT (user_id) DO NOTHING""", user_id, username)


async def get_profile(user_id: int) -> dict:
    row = await pg.fetchrow("""SELECT budget_max, rooms, area_min, property_type
        FROM users WHERE user_id=$1""", user_id)
    profile = dict(row) if row else {}
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
    return dict(budget_max=budget, area_min=area or None, rooms=rooms, property_type=kind)


async def save_profile(user_id: int, profile: dict) -> None:
    p = validate_profile(profile)
    await pg.execute("""INSERT INTO users
        (user_id, budget_max, rooms, area_min, property_type, notify_frequency)
        VALUES ($1,$2,$3,$4,$5,'off') ON CONFLICT (user_id) DO UPDATE SET
        budget_max=EXCLUDED.budget_max, rooms=EXCLUDED.rooms,
        area_min=EXCLUDED.area_min, property_type=EXCLUDED.property_type, updated_at=now()""",
        user_id, p['budget_max'], json.dumps(p['rooms']), p['area_min'], p['property_type'])


async def save_favorite(user_id: int, listing_id: str) -> bool:
    if not await pg.fetchrow('SELECT id FROM apartment_listings WHERE id=$1', listing_id):
        return False
    await ensure_user(user_id)
    await add_favorite(user_id, listing_id)
    return True
