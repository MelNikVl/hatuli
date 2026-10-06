"""Buyer map persistence and geometry; shared by Telegram and the web app."""
from __future__ import annotations

import json
import re
import secrets
import time

from bot.db import pg
from bot.core.geo import in_astana_bbox
from bot.core.hexgrid import hex_id, hex_center, hex_corners
from bot.core.buyer_store import ensure_user, validate_profile, save_profile

EDGE_M = 100
MAX_CELLS = 200
SESSION_SECONDS = 1800


class MapSessionExpired(ValueError):
    pass


def as_object(value) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def validate_selection(ids: list, edge_m: int = EDGE_M) -> dict:
    if edge_m != EDGE_M or not isinstance(ids, list) or not 1 <= len(ids) <= MAX_CELLS:
        raise ValueError(f'Выберите от 1 до {MAX_CELLS} участков со стороной 100 м.')
    for hid in ids:
        if not isinstance(hid, str) or not re.fullmatch(r'-?\d{1,4}:-?\d{1,4}', hid):
            raise ValueError('Неверный участок карты.')
        q, r = map(int, hid.split(':'))
        if hid != f'{q}:{r}' or not in_astana_bbox(*hex_center(hid, EDGE_M)):
            raise ValueError('Выберите участки в Астане.')
    return {'version': 1, 'edge_m': EDGE_M, 'hex_ids': sorted(set(ids))}


def cells(ids: list[str]) -> list[dict]:
    return [{'id': hid, 'corners': hex_corners(hid, EDGE_M)} for hid in ids]


def grid(south: float, west: float, north: float, east: float) -> list[dict]:
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise ValueError('Неверные границы карты.')
    if north - south > .06 or east - west > .10:
        raise ValueError('Приблизьте карту, чтобы выбирать участки.')
    coords = [list(map(int, hex_id(lat, lon, EDGE_M).split(':')))
              for lat in (south, north) for lon in (west, east)]
    qlo, qhi = min(p[0] for p in coords)-1, max(p[0] for p in coords)+1
    rlo, rhi = min(p[1] for p in coords)-1, max(p[1] for p in coords)+1
    if (qhi-qlo+1)*(rhi-rlo+1) > 1600:
        raise ValueError('Приблизьте карту, чтобы выбирать участки.')
    ids = []
    for q in range(qlo, qhi+1):
        for r in range(rlo, rhi+1):
            hid = f'{q}:{r}'
            lat, lon = hex_center(hid, EDGE_M)
            if in_astana_bbox(lat, lon) and south-.002 <= lat <= north+.002 and west-.003 <= lon <= east+.003:
                ids.append(hid)
    return cells(ids)


async def create_session(uid: int, profile: dict | None = None, center: dict | None = None) -> str:
    # Only the four completed preference fields, not transient FSM metadata.
    if profile is not None:
        profile = validate_profile({k: profile.get(k) for k in ('budget_max', 'rooms', 'area_min', 'property_type')})
    nonce = secrets.token_urlsafe(18)
    draft = {'nonce': nonce, 'expires': time.time()+SESSION_SECONDS, 'profile': profile}
    if center and in_astana_bbox(center.get('lat'), center.get('lon')):
        draft['center'] = [center['lat'], center['lon']]
    await ensure_user(uid)
    await pg.execute("""UPDATE users SET buyer_map=jsonb_set(buyer_map,'{draft}',$2::jsonb),
        updated_at=now() WHERE user_id=$1""", uid, json.dumps(draft))
    return nonce


async def cancel_session(uid: int, nonce: str) -> None:
    await pg.execute("""UPDATE users SET buyer_map=buyer_map-'draft'
        WHERE user_id=$1 AND buyer_map->'draft'->>'nonce'=$2""", uid, nonce)


async def session_completed(uid: int, nonce: str) -> bool:
    row = await pg.fetchrow("SELECT buyer_map->>'last_nonce' AS nonce FROM users WHERE user_id=$1", uid)
    return bool(row and row['nonce'] == nonce)


def check_draft(data: dict, nonce: str) -> dict:
    draft = data.get('draft') or {}
    if not nonce or draft.get('nonce') != nonce or draft.get('expires', 0) < time.time():
        raise MapSessionExpired('Ссылка устарела. Откройте карту заново через /map или /profile в боте.')
    return draft


async def load_session(uid: int, nonce: str) -> dict:
    row = await pg.fetchrow('SELECT buyer_map,location_lat,location_lon FROM users WHERE user_id=$1', uid)
    data = as_object(row['buyer_map']) if row else {}
    draft = check_draft(data, nonce)
    selection = data.get('selection') or {'version': 1, 'edge_m': EDGE_M, 'hex_ids': []}
    center = draft.get('center')
    if not center and selection['hex_ids']:
        center = hex_center(selection['hex_ids'][0], EDGE_M)
    if not center and row and in_astana_bbox(row['location_lat'], row['location_lon']):
        center = [row['location_lat'], row['location_lon']]
    return {'selection': selection, 'cells': cells(selection['hex_ids']),
            'center': center or [51.128, 71.43], 'max_cells': MAX_CELLS, 'edge_m': EDGE_M}


async def save_selection(uid: int, nonce: str, ids: list, edge_m: int = EDGE_M) -> dict:
    selection = validate_selection(ids, edge_m)
    # Serialize duplicate clicks, old tabs, and concurrent profile edits.
    async with pg.get_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow('SELECT buyer_map FROM users WHERE user_id=$1 FOR UPDATE', uid)
            data = as_object(row['buyer_map']) if row else {}
            if not data.get('draft') and data.get('last_nonce') == nonce and data.get('selection') == selection:
                return {'count': len(selection['hex_ids']), 'already_saved': True}
            draft = check_draft(data, nonce)
            if draft.get('profile') is not None:
                await save_profile(uid, draft['profile'], execute=conn.execute)
            data.pop('draft', None)
            data.update(selection=selection, last_nonce=nonce)
            await conn.execute("""UPDATE users SET buyer_map=$2::jsonb,
                location_lat=NULL,location_lon=NULL,radius_km=NULL,updated_at=now()
                WHERE user_id=$1""", uid, json.dumps(data))
    return {'count': len(selection['hex_ids']), 'already_saved': False}
