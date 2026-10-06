"""Integration against a disposable PostgreSQL database (never production)."""
import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from bot.core import buyer, buyer_store
from bot.db import pg


@pytest_asyncio.fixture
async def seeded():
    dsn = os.getenv('DATABASE_URL', '')
    if 'test' not in dsn.rsplit('/', 1)[-1]:
        pytest.skip('requires an explicitly configured disposable test database')
    await pg.init_pool(dsn)
    uid = -int(str(uuid.uuid4().int)[:12])
    lids = [str(uuid.uuid4().int)[:16] for _ in range(2)]
    try:
        for n, lid in enumerate(lids):
            await pg.execute('''INSERT INTO apartment_listings
                (id,url,price,area,rooms,lat,lon,floor,floors_total,market_type,is_active)
                VALUES ($1,$2,$3,$4,2,51.13,71.43,5,12,'secondary',TRUE)''',
                lid, f'https://krisha.kz/a/show/{lid}', 32_000_000 - n*2_000_000, 52. + n*3)
        yield uid, lids
    finally:
        await pg.execute('DELETE FROM favorites WHERE user_id=$1', uid)
        await pg.execute('DELETE FROM users WHERE user_id=$1', uid)
        await pg.execute('DELETE FROM price_history WHERE listing_id=ANY($1::text[])', lids)
        await pg.execute('DELETE FROM apartment_listings WHERE id=ANY($1::text[])', lids)
        await pg.close_pool()


@pytest.mark.asyncio
async def test_profile_and_favorites_round_trip(seeded):
    uid, lids = seeded
    await buyer_store.ensure_user(uid, 'buyer_test')
    await pg.execute("UPDATE users SET notify_frequency='weekly' WHERE user_id=$1", uid)
    profile = dict(budget_max=35_000_000, rooms=[2, 4], area_min=50., property_type='secondary')
    await buyer_store.save_profile(uid, profile)
    assert await buyer_store.get_profile(uid) == profile
    assert await buyer_store.save_favorite(uid, lids[0])
    assert await buyer_store.save_favorite(uid, lids[0])
    assert await pg.fetchval('SELECT count(*) FROM favorites WHERE user_id=$1', uid) == 1
    assert await pg.fetchval('SELECT notify_frequency FROM users WHERE user_id=$1', uid) == 'weekly'


@pytest.mark.asyncio
async def test_full_core_uses_real_detail_and_similar_search(seeded):
    _, lids = seeded
    with patch.object(buyer, 'import_listing', AsyncMock()) as scrape, \
         patch.object(buyer, 'compute_dom_scenario_cached', AsyncMock(return_value={'available': False})):
        result = await buyer.analyze_for_buyer(lids[0])
    scrape.assert_not_called()
    assert result['found']
    assert result['listing']['market'] == 'secondary'
    assert result['listing']['is_active'] is True
    assert result['better_nearby'][0]['listing']['id'] == lids[1]
    assert result['better_nearby'][0]['rank_points'] >= 3
    assert result['score'] is None  # do not fabricate Deal Score for a fresh row


@pytest.mark.asyncio
async def test_geolocation_profile_round_trip_and_legacy_update_preserves_point(seeded):
    uid, _ = seeded
    profile = dict(budget_max=35_000_000, rooms=[1], area_min=30., property_type='secondary',
                   location_lat=51.13, location_lon=71.43, radius_km=2)
    await buyer_store.save_profile(uid, profile)
    loaded = await buyer_store.get_profile(uid)
    assert loaded['location_lat'] == pytest.approx(51.13)
    assert loaded['location_lon'] == pytest.approx(71.43)
    assert loaded['radius_km'] == 2
    # A caller using the earlier four-field contract must not clear location.
    await buyer_store.save_profile(uid, dict(budget_max=30_000_000, rooms=[1], area_min=30., property_type=None))
    loaded = await buyer_store.get_profile(uid)
    assert loaded['radius_km'] == 2 and loaded['location_lat'] == pytest.approx(51.13)


@pytest.mark.asyncio
async def test_actual_price_events_visible_in_buyer_detail(seeded):
    from bot.buyer.telegram import render_summary
    _, lids = seeded
    await pg.execute("""INSERT INTO price_history (listing_id, old_price, new_price, changed_at)
        VALUES ($1,34000000,32000000,'2026-10-06T00:00:00Z')""", lids[0])
    detail = await buyer._detail(lids[0])
    result = buyer.summarize(detail)
    assert result['price_history']['changes'] == 1
    assert '−2 000 000 ₸' in render_summary(result)
    assert '06.10.2026' in render_summary(result)


@pytest.mark.asyncio
async def test_map_selection_atomic_round_trip_and_stale_tabs(seeded):
    from bot.core import buyer_map as bm
    uid, _ = seeded
    old=dict(budget_max=35_000_000, rooms=[2], area_min=50., property_type=None,
             location_lat=51.13,location_lon=71.43,radius_km=2)
    new=dict(budget_max=25_000_000, rooms=[1], area_min=30., property_type='secondary')
    await buyer_store.save_profile(uid,old)
    nonce=await bm.create_session(uid,new)
    assert (await buyer_store.get_profile(uid))['budget_max']==old['budget_max']
    assert (await bm.load_session(uid,nonce))['selection']['hex_ids']==[]
    with pytest.raises(bm.MapSessionExpired): await bm.save_selection(uid-1,nonce,['0:0'])
    assert (await bm.save_selection(uid,nonce,['0:0','1:0']))['count']==2
    profile=await buyer_store.get_profile(uid)
    assert profile['budget_max']==new['budget_max'] and profile['area_min']==30
    assert 'radius_km' not in profile
    assert profile['buyer_area']['hex_ids']==['0:0','1:0']
    assert await bm.session_completed(uid,nonce)
    assert (await bm.save_selection(uid,nonce,['1:0','0:0']))['already_saved']
    with pytest.raises(bm.MapSessionExpired): await bm.save_selection(uid,nonce,['2:0'])
    first=await bm.create_session(uid)
    second=await bm.create_session(uid)
    with pytest.raises(bm.MapSessionExpired): await bm.save_selection(uid,first,['2:0'])
    await bm.save_selection(uid,second,['2:0'])
    assert (await buyer_store.get_profile(uid))['budget_max']==new['budget_max']
    pending=await bm.create_session(uid)
    await bm.cancel_session(uid,pending)
    with pytest.raises(bm.MapSessionExpired): await bm.save_selection(uid,pending,['3:0'])
    await buyer_store.save_profile(uid,old)
    assert 'buyer_area' not in await buyer_store.get_profile(uid)
