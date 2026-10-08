import hashlib
import hmac
import json
import time
from urllib.parse import urlencode
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from bot.core import buyer_map as bm, buyer
from bot.core.hexgrid import hex_id, hex_center
from bot.buyer.map_web import router


def signed(token, uid=123, age=0):
    data = {'auth_date': str(int(time.time()-age)), 'user': json.dumps({'id': uid, 'first_name': 'Test', 'is_bot': False})}
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    data['hash'] = hmac.new(secret, '\n'.join(f'{k}={v}' for k,v in sorted(data.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def test_geometry_and_selection():
    hid = hex_id(51.128, 71.43, 100)
    assert hid == '0:0'
    assert bm.validate_selection([hid, hid])['hex_ids'] == [hid]
    result = bm.grid(51.12, 71.42, 51.14, 71.44)
    assert hid in [c['id'] for c in result]
    for cell in result:
        assert hex_id(*hex_center(cell['id'], 100), 100) == cell['id']
        assert len(cell['corners']) == 6
    for ids, size in [([],100),([hid],200),(['00:0'],100),(['9999:9999'],100),([hid]*201,100)]:
        with pytest.raises(ValueError): bm.validate_selection(ids,size)
    with pytest.raises(ValueError): bm.grid(50,70,52,73)
    with pytest.raises(ValueError): bm.grid(float('nan'),71,52,72)


@pytest.mark.asyncio
async def test_api_checks_telegram_signature_age_and_owner(monkeypatch):
    token='123:fake-test-token'
    monkeypatch.setenv('BUYER_BOT_TOKEN',token)
    app=FastAPI();app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        for auth in ['', 'tma '+signed('wrong'), 'tma '+signed(token,age=4000), 'tma '+signed(token,age=-100), 'tma '+signed(token,uid=-5)]:
            assert (await client.get('/buyer/map/api/session?nonce=x',headers={'Authorization':auth})).status_code==401
        with patch.object(bm,'save_selection',AsyncMock(return_value={'count':1,'already_saved':False})) as save:
            r=await client.post('/buyer/map/api/save',headers={'Authorization':'tma '+signed(token)},json={'nonce':'n','hex_ids':['0:0'],'edge_m':100,'user_id':999})
            assert r.status_code==200
            save.assert_awaited_once_with(123,'n',['0:0'],100)
        with patch.object(bm,'load_session',AsyncMock(side_effect=bm.MapSessionExpired('expired'))):
            assert (await client.get('/buyer/map/api/session?nonce=old',headers={'Authorization':'tma '+signed(token)})).status_code==409


def test_selected_hexes_replace_radius_and_missing_coordinates_are_neutral():
    profile={'buyer_area':bm.validate_selection(['0:0']), 'location_lat':51.128,'location_lon':71.43,'radius_km':10}
    assert buyer.has_location(profile)
    assert buyer.profile_mismatches({'lat':51.128,'lon':71.43},profile)==[]
    lat,lon=hex_center('2:0',100)
    assert buyer.profile_mismatches({'lat':lat,'lon':lon},profile)==['Квартира вне выбранных на карте участков']
    assert buyer.profile_mismatches({},profile)==[]


@pytest.mark.asyncio
async def test_nearby_map_returns_only_requested_public_listing_fields():
    from bot.db import pg
    app=FastAPI();app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        for ids in ['','12345,abc','1,2,3,4,5','12345 OR 1=1']:
            assert (await client.get('/buyer/map/api/nearby',params={'ids':ids})).status_code==400
        rows=[dict(id='123456',lat=51.128,lon=71.43,price=25000000,area=31,rooms=1,complex_name='A',address=None,is_active=True),
              dict(id='123457',lat=51.13,lon=71.43,price=26000000,area=32,rooms=1,complex_name='B',address=None,is_active=False)]
        with patch.object(pg,'fetch',AsyncMock(return_value=list(reversed(rows)))) as fetch:
            result=(await client.get('/buyer/map/api/nearby?ids=123456,123457,123458')).json()
        assert [r['id'] for r in result['items']]==['123456','123457']
        assert result['items'][0]['index']==0 and result['items'][1]['distance_m']>0
        assert result['items'][1]['is_active'] is False and result['missing']==1
        assert fetch.await_args.args[1]==['123456','123457','123458']
