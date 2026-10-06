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
