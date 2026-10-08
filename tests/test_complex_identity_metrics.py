import os
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_current_metrics_follow_ids_umbrella_and_trim_coordinate_outlier():
    from bot.db import pg
    from bot.core.complex_metrics import CURRENT_METRICS_SQL, CENTROID_SQL
    from bot.core.complex_membership import listing_complex_match_sql
    await pg.init_pool(os.environ['DATABASE_URL'])
    prefix = '__test_identity_metrics_' + uuid4().hex
    ids = []
    try:
        root = await pg.fetchval('INSERT INTO complexes(name,is_umbrella) VALUES($1,TRUE) RETURNING id', prefix)
        child = await pg.fetchval('INSERT INTO complexes(name,parent_complex_id) VALUES($1,$2) RETURNING id', prefix+' child', root)
        alias = await pg.fetchval("INSERT INTO complexes(name,canonical_id,canonical_reason) VALUES($1,$2,'krisha_slug') RETURNING id", prefix+' alias', child)
        ids = [root, child, alias]
        for i in range(4):
            await pg.execute('INSERT INTO apartment_listings(id,complex_name,complex_id,price,area,yield_pct,is_active,lat,lon) VALUES($1,$2,$3,$4,50,8,TRUE,$5,71.4)',
                prefix+str(i), 'stale name of another project', alias, 30000000, 51.1 if i < 3 else 51.3)
        await pg.execute(CURRENT_METRICS_SQL)
        rows = await pg.fetch('SELECT id,listings_count,avg_price_m2,avg_yield FROM complexes WHERE id=ANY($1::int[])', ids)
        rows = {r['id']: dict(r) for r in rows}
        assert rows[root]['listings_count'] == rows[child]['listings_count'] == 4
        assert rows[alias]['listings_count'] == 0
        assert rows[child]['avg_price_m2'] == pytest.approx(600000)
        assert rows[root]['avg_yield'] == pytest.approx(8)
        assert await pg.fetchval('SELECT count(*) FROM apartment_listings a WHERE '+listing_complex_match_sql('$1','a',include_children=True),root) == 4
        await pg.execute(CENTROID_SQL)
        assert await pg.fetchval('SELECT lat FROM complexes WHERE id=$1',child) == pytest.approx(51.1)
        assert await pg.fetchval('SELECT lat FROM complexes WHERE id=$1',alias) is None
    finally:
        await pg.execute('DELETE FROM apartment_listings WHERE id LIKE $1', prefix+'%')
        if ids:
            await pg.execute('DELETE FROM complexes WHERE id=ANY($1::int[])', ids)
        await pg.close_pool()
