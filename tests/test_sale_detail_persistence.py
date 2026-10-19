import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from bot.core.sale_detail_persistence import (DETAIL_UPDATE_SQL, catalog_only_row, save_source_details,
                                             persist_actual_attempts, analyze_with_attempt_capture)

NOW = datetime.now(timezone.utc)


def observed(**values):
    return {'id': '123456789', 'price': 30000000, 'title': '2-комнатная квартира',
            'floor': 8, 'description': 'Свежее описание',
            '_source_details_checked_at': NOW.isoformat(),
            '_source_details_attempted_at': NOW.isoformat(), '_source_detail_values': values}


def test_catalog_write_defers_observed_fields_and_removes_placeholder_detail_values():
    row = observed(floor=9, furniture=False, description='')
    row.update(year_built=2020, building_type='монолитный', ceiling_height=3.0, details_fetched=True)
    catalog = catalog_only_row(row)
    assert 'floor' not in catalog and 'description' not in catalog
    assert 'year_built' not in catalog and 'building_type' not in catalog and 'ceiling_height' not in catalog
    assert catalog['details_fetched'] is False
    assert catalog['price'] == 30000000


def test_catalog_floor_observed_in_feed_is_not_lost_when_detail_did_not_observe_it():
    assert catalog_only_row(observed(furniture=False))['floor'] == 8


@pytest.mark.asyncio
async def test_legacy_success_bool_and_unmasked_values_never_backfill_check_dates():
    execute = AsyncMock()
    row = {'id': '123456789', 'details_fetched': True, 'floor': 8,
           '_source_detail_values': {'description': 'old'}}
    assert await save_source_details(row, execute=execute) is None
    assert not execute.called


@pytest.mark.asyncio
async def test_fresh_values_null_empty_false_and_timestamp_are_one_atomic_statement():
    execute = AsyncMock(return_value='UPDATE 1')
    row = observed(floor=None, description='', furniture=False, views_count=0, score_total=999)
    await save_source_details(row, execute=execute)
    sql, identifier, patch, checked, attempted = execute.call_args.args
    assert execute.await_count == 1 and identifier == row['id']
    assert json.loads(patch) == {'floor': None, 'description': '', 'furniture': False, 'views_count': 0}
    assert checked == attempted == NOW
    assert "AND $2::jsonb ? 'floor'" in sql
    assert 'source_details_checked_at=GREATEST' in sql
    assert 'source_details_checked_at <= $3::timestamptz' in sql
    assert 'last_seen=' not in sql


@pytest.mark.asyncio
async def test_attempt_only_does_not_date_or_replace_old_attributes():
    execute = AsyncMock(return_value='UPDATE 1')
    await save_source_details({'id': '123456789', '_source_details_attempted_at': NOW.isoformat(),
                               '_source_detail_values': {'floor': 9}}, execute=execute)
    assert json.loads(execute.call_args.args[2]) == {}
    assert execute.call_args.args[3] is None and execute.call_args.args[4] == NOW


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['2026-10-19', 'not-a-date', (NOW + timedelta(days=2)).isoformat()])
async def test_invalid_or_future_timestamp_is_not_a_fresh_success(value):
    execute = AsyncMock()
    assert await save_source_details({'id': '123456789', '_source_details_checked_at': value}, execute=execute) is None
    assert not execute.called


@pytest.mark.asyncio
async def test_failed_atomic_update_does_not_make_a_separate_success_write():
    execute = AsyncMock(side_effect=RuntimeError('write failed'))
    with pytest.raises(RuntimeError, match='write failed'):
        await save_source_details(observed(floor=9), execute=execute)
    assert execute.await_count == 1


@pytest.mark.asyncio
async def test_update_zero_is_not_a_successful_persistence_receipt():
    with pytest.raises(RuntimeError, match='not persisted'):
        await save_source_details(observed(floor=9), execute=AsyncMock(return_value='UPDATE 0'))


@pytest.mark.asyncio
async def test_cancellation_records_actual_attempts_only_and_preserves_cancel(monkeypatch):
    from bot.db import pg
    execute = AsyncMock(return_value='UPDATE 1')
    monkeypatch.setattr(pg, 'execute', execute)
    async def cancel(*args, **kwargs):
        exc = asyncio.CancelledError()
        exc.sale_detail_attempts = {'222222222': NOW.isoformat()}
        raise exc
    previous = [{'id': '111111111', '_source_details_attempted_at': NOW.isoformat()}]
    with pytest.raises(asyncio.CancelledError):
        await analyze_with_attempt_capture(cancel, prior_results=previous)
    assert execute.await_count == 2
    for call in execute.call_args_list:
        assert 'source_details_attempted_at=' in call.args[0]
        assert 'source_details_checked_at' not in call.args[0] and 'last_seen' not in call.args[0]
        assert not call.args[0].startswith('INSERT')


@pytest.mark.asyncio
async def test_cooldown_without_actual_http_does_not_create_attempt_row():
    execute = AsyncMock()
    await persist_actual_attempts({}, execute=execute)
    assert not execute.called


@pytest.mark.asyncio
@pytest.mark.parametrize('new', [False, True])
async def test_manual_sweep_saves_catalog_then_atomic_fresh_patch(monkeypatch, new):
    from bot.db import pg
    import full_sweep
    monkeypatch.setattr(pg, 'fetchrow', AsyncMock(return_value=None if new else {'id': '123456789', 'price': 30000000}))
    async def write(sql, *args):
        return 'INSERT 0 1' if 'INSERT INTO apartment_listings' in sql else 'UPDATE 1'
    execute = AsyncMock(side_effect=write)
    monkeypatch.setattr(pg, 'execute', execute)
    assert await full_sweep.upsert(observed(floor=9, description='новое', furniture=False)) == ('new' if new else 'upd')
    assert execute.await_count == 2
    assert execute.call_args_list[-1].args[0] == DETAIL_UPDATE_SQL
    assert json.loads(execute.call_args_list[-1].args[2])['furniture'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('detail_failure', [False, True])
async def test_main_collector_acknowledges_only_after_fresh_patch_save(monkeypatch, detail_failure):
    from bot.db import pg, settings
    from bot.core import complex_audit
    import service_apartments as service
    source = observed(floor=9, description='новое')
    monkeypatch.setattr(settings, 'load', AsyncMock())
    monkeypatch.setattr(settings, 'set', AsyncMock())
    monkeypatch.setattr(complex_audit, 'street_names', AsyncMock(return_value=set()))
    monkeypatch.setattr(pg, 'fetchrow', AsyncMock(return_value={'id': source['id'], 'price': source['price']}))
    async def write(sql, *args):
        if sql == DETAIL_UPDATE_SQL and detail_failure:
            raise RuntimeError('detail patch unavailable')
        return 'UPDATE 1'
    execute = AsyncMock(side_effect=write)
    monkeypatch.setattr(pg, 'execute', execute)
    monkeypatch.setattr(service, '_collect_sale_catalog', AsyncMock(return_value=([source], None, [], {'blocked': True}, {})))
    commit = AsyncMock()
    monkeypatch.setattr(service, '_commit_sale_catalog', commit)
    await service.run_cycle()
    assert commit.call_args.args[3] == (set() if detail_failure else {source['id']})
    sql = execute.call_args_list[0].args[0]
    assert 'floor=COALESCE($7, floor)' in sql and 'ELSE details_fetched END' in sql
    assert execute.call_args_list[0].args[7] is None  # deferred fresh floor


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['older', 'fresh', 'failed-save', 'failed-snapshot'])
async def test_main_cycle_never_replays_rejected_or_unsaved_source_details(monkeypatch, mode):
    """Exercise the real post-save loops, not just the atomic SQL helper."""
    from bot.db import pg, settings
    from bot.core import complex_audit, house_resolution, listing_intel
    import service_apartments as service

    stored = {'id': '123456789', 'price': 30000000, 'title': 'Квартира',
              'address': 'Сохранённый адрес', 'description': 'stored description',
              'lat': 51.1, 'lon': 71.4, 'photos': ['stored.jpg'], 'seller_name': 'stored seller',
              'is_urgent': False, 'complex_name': 'Зонтик', 'complex_url': 'stored complex',
              'year_built': 2020, 'source_details_checked_at': NOW, 'last_seen': NOW,
              'is_active': True, 'geo_source': 'krisha'}
    checked = NOW + timedelta(seconds=1) if mode == 'fresh' else NOW - timedelta(hours=1)
    source = observed(description='от застройщика unsafe description', lat=52.2, lon=72.2,
                      photos=['source.jpg'], seller_name='source seller', is_urgent=True,
                      year_built=2099)
    source.update(description='от застройщика unsafe description', lat=52.2, lon=72.2,
                  photos=['source.jpg'], seller_name='source seller', is_urgent=True,
                  year_built=2099, complex_name='Зонтик', complex_url='unsafe complex', is_archived=True,
                  _source_details_checked_at=checked.isoformat())
    monkeypatch.setattr(settings, 'load', AsyncMock())
    monkeypatch.setattr(settings, 'set', AsyncMock())
    monkeypatch.setattr(settings, 'get_int', lambda key, default=0: 0 if key == 'COORD_BACKFILL_BATCH' else default)
    monkeypatch.setattr(complex_audit, 'street_names', AsyncMock(return_value=set()))
    monkeypatch.setattr(service, '_collect_sale_catalog', AsyncMock(return_value=([source], None, [], {}, {})))
    monkeypatch.setattr(service, '_commit_sale_catalog', AsyncMock())

    async def get(sql, *args):
        return {'id': 123} if 'FROM complexes' in sql else {'id': stored['id'], 'price': stored['price']}
    monkeypatch.setattr(pg, 'fetchrow', AsyncMock(side_effect=get))
    snapshots = []
    async def fetch(sql, *args):
        if 'WHERE id=ANY($1::text[])' in sql:
            assert args == ([stored['id']],)
            if mode == 'failed-snapshot':
                raise RuntimeError('snapshot unavailable')
            snapshots.append(dict(stored))
            return [dict(stored)]
        if 'primary_score_total IS NULL' in sql:
            # End after all three real derived loops, before unrelated jobs.
            raise asyncio.CancelledError()
        return []
    monkeypatch.setattr(pg, 'fetch', AsyncMock(side_effect=fetch))
    async def write(sql, *args):
        if sql == DETAIL_UPDATE_SQL:
            if mode == 'failed-save':
                raise RuntimeError('atomic write unavailable')
            if args[2] >= stored['source_details_checked_at']:
                stored.update(json.loads(args[1]))
                stored['source_details_checked_at'] = args[2]
        return 'UPDATE 1'
    execute = AsyncMock(side_effect=write)
    monkeypatch.setattr(pg, 'execute', execute)
    children = AsyncMock(return_value=[{'id': 456}])
    resolve = AsyncMock(return_value={'house_id': 456, 'method': 'address', 'detail': 'stored evidence'})
    monkeypatch.setattr(house_resolution, 'get_umbrella_children', children)
    monkeypatch.setattr(house_resolution, 'resolve_house', resolve)
    finish_inputs = []
    def finish(title, description):
        finish_inputs.append(description)
        return 'ready', 0, 'готово'
    monkeypatch.setattr(listing_intel, 'detect_finish_level', finish)
    with pytest.raises(asyncio.CancelledError):
        await service.run_cycle()

    assert sum(c.args[0] == DETAIL_UPDATE_SQL for c in execute.call_args_list) == 1
    assert stored['complex_url'] == 'stored complex' and stored['is_active'] is True
    for call in execute.call_args_list:
        sql = call.args[0]
        assert not sql.startswith(('UPDATE apartment_listings SET lat=',
                                   'UPDATE apartment_listings SET photos=',
                                   'UPDATE apartment_listings SET seller_name=',
                                   'UPDATE apartment_listings SET complex_url=',
                                   'UPDATE apartment_listings SET is_active=FALSE'))
    derived = [c for c in execute.call_args_list if any(key in c.args[0] for key in
               ('SET resolved_house_id=', 'SET finish_level=', 'SET market_type='))]
    if mode in ('failed-save', 'failed-snapshot'):
        assert not derived and not resolve.called and not finish_inputs
    else:
        expected = source['description'] if mode == 'fresh' else 'stored description'
        assert resolve.call_args.kwargs['listing_description'] == expected
        assert resolve.call_args.kwargs['listing_lat'] == stored['lat']
        assert finish_inputs == [expected]
        assert len(derived) == 3
        for call in derived:
            assert 'source_details_checked_at IS NOT DISTINCT FROM' in call.args[0]
            assert 'last_seen IS NOT DISTINCT FROM' in call.args[0]
            assert call.args[-2:] == (stored['source_details_checked_at'], NOW)
        market = next(c for c in derived if 'SET market_type=' in c.args[0])
        assert market.args[2] == ('primary' if mode == 'fresh' else 'secondary')
    assert stored['photos'] == (['source.jpg'] if mode == 'fresh' else ['stored.jpg'])
    assert stored['seller_name'] == ('source seller' if mode == 'fresh' else 'stored seller')
    assert stored['is_urgent'] is (mode == 'fresh')


@pytest.mark.asyncio
async def test_head_and_deep_share_the_configured_actual_http_budget():
    from service_apartments import _collect_sale_catalog
    class Settings:
        def __init__(self):
            self.data = {'PARSER_MAX_PAGES': '1', 'DEEP_SWEEP_BATCH': '2', 'DETAIL_FETCH_BATCH': '5'}
        def get(self, key, default=None):
            return self.data.get(key, default)
        def get_int(self, key, default=0):
            return int(self.get(key, default))
        def get_float(self, key, default=0):
            return float(self.get(key, default))
        async def set(self, key, value):
            self.data[key] = str(value)
    calls = []
    async def analyze(city, *, max_pages, stats, start_page=1):
        calls.append(stats['_detail_budget_remaining'])
        stats['_detail_budget_remaining'] -= 3 if start_page == 1 else 2
        stats['page_reports'] = [{'page': start_page, 'ids': ['100000'], 'accepted_ids': ['100000'],
                                  'checked_at': NOW.isoformat(), 'total_count': 60, 'expected_last_page': 3}]
        return []
    await _collect_sale_catalog(Settings(), analyze, now=NOW)
    assert calls == [5, 2]


def test_migration_adds_nullable_timestamps_without_fake_backfill():
    sql = (Path(__file__).parents[1] / 'migrations/113_sale_detail_observation_times.sql').read_text()
    assert 'source_details_checked_at TIMESTAMPTZ' in sql
    assert 'source_details_attempted_at TIMESTAMPTZ' in sql
    assert 'DEFAULT' not in sql and 'UPDATE ' not in sql and 'NOT NULL' not in sql
