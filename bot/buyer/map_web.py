"""Telegram Mini App: authenticated map preferences, no bot token in the browser."""
import os
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram.utils.web_app import safe_parse_webapp_init_data
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from bot.core import buyer_map
from bot.core.buyer_locations import search_locations

router = APIRouter(prefix='/buyer/map')
ROOT = Path(__file__).resolve().parents[2]


def authenticate(request: Request) -> int:
    raw = request.headers.get('authorization', '')
    if not raw.startswith('tma ') or len(raw) > 16384:
        raise HTTPException(401, 'Откройте карту из Telegram-бота.')
    raw = raw[4:]
    try:
        pairs = parse_qsl(raw)
        if len(dict(pairs)) != len(pairs):
            raise ValueError()
        data = safe_parse_webapp_init_data(os.environ['BUYER_BOT_TOKEN'], raw)
        age = time.time() - data.auth_date.timestamp()
        if not -30 <= age <= 3600 or not data.user or data.user.is_bot or data.user.id <= 0:
            raise ValueError()
        return data.user.id
    except (ValueError, KeyError, TypeError):
        raise HTTPException(401, 'Сессия Telegram устарела. Откройте /map заново.') from None


def failure(exc):
    raise HTTPException(409 if isinstance(exc, buyer_map.MapSessionExpired) else 400, str(exc))


@router.get('')
async def page():
    return FileResponse(ROOT / 'bot/templates/buyer_map.html', headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'origin'})


@router.get('/api/grid')
async def grid(south: float, west: float, north: float, east: float):
    try:
        return {'cells': buyer_map.grid(south, west, north, east)}
    except ValueError as exc:
        failure(exc)


@router.get('/api/session')
async def session(request: Request, nonce: str):
    uid = authenticate(request)
    try:
        return JSONResponse(await buyer_map.load_session(uid, nonce), headers={'Cache-Control': 'no-store'})
    except ValueError as exc:
        failure(exc)


@router.post('/api/session')
async def renew(request: Request, nonce: str):
    uid = authenticate(request)
    try:
        return JSONResponse(await buyer_map.renew_session(uid, nonce), headers={'Cache-Control': 'no-store'})
    except ValueError as exc:
        failure(exc)


@router.get('/api/search')
async def search(request: Request, nonce: str, q: str, kind: str = 'complex'):
    uid = authenticate(request)
    try:
        await buyer_map.load_session(uid, nonce)
        if kind not in ('complex', 'address') or not 2 <= len(q) <= 150:
            raise ValueError('Введите название ЖК или адрес, от 2 до 150 символов.')
        return JSONResponse({'results': await search_locations(q, kind)}, headers={'Cache-Control': 'no-store'})
    except ValueError as exc:
        failure(exc)


@router.post('/api/save')
async def save(request: Request):
    uid = authenticate(request)
    body = await request.body()
    if len(body) > 16384:
        raise HTTPException(413, 'Слишком много участков.')
    try:
        import json
        data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get('nonce'), str):
            raise ValueError('Неверный запрос.')
        result = await buyer_map.save_selection(uid, data['nonce'], data.get('hex_ids'), data.get('edge_m'))
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})
    except (ValueError, TypeError) as exc:
        failure(exc)


@router.get('/nearby')
async def nearby_page():
    return FileResponse(ROOT / 'bot/templates/buyer_nearby.html', headers={
        'Cache-Control': 'no-store', 'Referrer-Policy': 'origin'})


@router.get('/api/nearby')
async def nearby_data(ids: str):
    # Public listing facts only. No user profile or saved search is exposed.
    import re
    from bot.db import pg
    from bot.core.geo import in_astana_bbox, haversine_km
    values = ids.split(',')
    if not 1 <= len(values) <= 4 or any(not re.fullmatch(r'\d{5,20}', v) for v in values):
        raise HTTPException(400, 'Неверная ссылка. Откройте карту из результата анализа в боте.')
    values = list(dict.fromkeys(values))
    rows = await pg.fetch('''SELECT id,lat,lon,price,area,rooms,complex_name,address,is_active
        FROM apartment_listings WHERE id=ANY($1::text[])''', values)
    found = {str(r['id']): dict(r) for r in rows if in_astana_bbox(r['lat'], r['lon'])}
    base = found.get(values[0])
    items = []
    for index, lid in enumerate(values):
        row = found.get(lid)
        if not row:
            continue
        row['index'] = index
        row['distance_m'] = (round(haversine_km(base['lat'],base['lon'],row['lat'],row['lon'])*100)
                             *10 if base and index else None)
        items.append(row)
    from fastapi.encoders import jsonable_encoder
    return JSONResponse(jsonable_encoder({'items':items,'missing':len(values)-len(items)}),
                        headers={'Cache-Control':'no-store'})
