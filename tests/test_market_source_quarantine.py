"""Mocked regression proof for a durable negative source hold; no live DB."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest

from bot.core.market_comparison import build_market_snapshot, compare_market, load_market_snapshot
from bot.core.sale_source_quarantine import KNOWN_SOURCE_CONFLICTS, quarantine_sale_rows
from scripts import publish_market_comparison as publisher
from tests.test_market_comparison import NOW, primary, secondary, snapshot

CONFLICT_ID = 1012143050


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://unused:unused@127.0.0.1:1/quarantine_test')

    async def reject(*args, **kwargs):
        raise AssertionError('Quarantine tests must not connect to a real database')

    monkeypatch.setattr('asyncpg.connect', reject)
    monkeypatch.setattr('asyncpg.create_pool', reject)
    monkeypatch.setattr('bot.db.pg.init_pool', reject)


def test_negative_ledger_provenance_is_not_a_new_positive_source_receipt():
    ledger = KNOWN_SOURCE_CONFLICTS[0]
    assert ledger['id'] == str(CONFLICT_ID)
    assert ledger['status'] == 'unresolved_negative_evidence'
    assert ledger['observed_date'] == '2026-10-19'
    assert ledger['time_precision'] == 'date_only_for_source_complex_name'
    assert ledger['listing_receipt_contains_source_complex_name'] is False
    assert 'observed_at' not in ledger and 'checked_at' not in ledger
    assert ledger['quarantined_property_ids'] == [12014]
    assert ledger['property_link_evidence']['property_identity_status'] == 'provisional'


def test_known_conflict_and_any_joined_copy_of_its_property_relist_leave_both_medians():
    good = [secondary(100000 + i, price=30_000_000 + i * 100_000) for i in range(5)]
    conflict = secondary(CONFLICT_ID, property_id=77, complex_name='Возрождение', price=90_000_000)
    relist = secondary(999999, property_id=77, price=90_000_000)
    alternate_join = secondary(999999, property_id=88, price=90_000_000)
    supplemental = dict(relist, property_id=None, comparison_only=True, public_comparison_listing=True)
    rows = good + [conflict, relist, alternate_join, supplemental]
    original = deepcopy(rows)
    result = build_market_snapshot([primary(i + 1) for i in range(5)], rows, now=NOW)
    expected = build_market_snapshot([primary(i + 1) for i in range(5)], good, now=NOW)
    assert result == expected
    assert not result['listing_details']
    assert rows == original
    actual_stats = compare_market(result, scope='city', now=NOW)['stats']['secondary']
    expected_stats = compare_market(expected, scope='city', now=NOW)['stats']['secondary']
    assert actual_stats['median_total'] == expected_stats['median_total']
    assert actual_stats['median_per_m2'] == expected_stats['median_per_m2']


def test_known_source_without_a_property_key_is_still_quarantined():
    assert quarantine_sale_rows([secondary(CONFLICT_ID)]) == []
    good = secondary(999999)
    assert quarantine_sale_rows([good], linked_listing_ids=[good['id']]) == []


def test_frozen_property_hold_survives_unlink_or_reassignment_of_original_source():
    # Retain the negative hold on the observed provisional key. It is never
    # renamed a confirmed physical identity or transferred as positive proof.
    rows = [secondary(999999, property_id=12014), secondary(CONFLICT_ID, property_id=88), secondary(100000)]
    assert [row['id'] for row in quarantine_sale_rows(rows)] == ['100000']


def test_old_public_snapshot_hides_known_negative_without_faking_freshness(tmp_path):
    old = snapshot()
    bad = deepcopy(next(row for row in old['offers'] if row['market_type'] == 'secondary'))
    bad.update(id=str(CONFLICT_ID), url=f'https://krisha.kz/a/show/{CONFLICT_ID}')
    old['offers'].append(bad)
    old['listing_details'] = [dict(bad, current_price=bad['price'])]
    old['summary']['secondary_count'] += 1
    # The direct comparison consumer must also omit a known ID in old input.
    assert compare_market(old, scope='city', now=NOW)['stats']['secondary']['count'] == 5
    path = tmp_path / 'old-market.json'
    path.write_text(json.dumps(old), encoding='utf-8')
    previous = path.read_bytes()
    safe = load_market_snapshot(path)
    assert safe['generated_at'] == old['generated_at']
    assert safe['summary']['secondary_count'] == 5
    assert all(row['id'] != str(CONFLICT_ID) for row in safe['offers'])
    assert safe['listing_details'] == []
    assert path.read_bytes() == previous


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_reader_expands_hold_for_inactive_original_before_history_estimates_and_details(monkeypatch):
    relist_id = '999999'
    relist = secondary(int(relist_id), property_id=77, price=90_000_000)
    good = secondary(100000)
    detail = dict(relist, property_id=None)
    calls = []

    async def fetch(sql, *args):
        calls.append((sql, args))
        if sql == publisher.PRIMARY_SQL:
            return [primary()]
        if sql == publisher.SECONDARY_SQL:
            # The original source no longer appears in the active slice.
            return [relist, good]
        if sql == publisher.COMPARISON_DETAILS_SQL:
            return [detail]
        if sql == publisher.QUARANTINED_LINKS_SQL:
            assert args == ([str(CONFLICT_ID)], [12014], publisher.MAX_QUARANTINE_LINKS + 1)
            return [{'id': str(CONFLICT_ID)}, {'id': relist_id}]
        if sql == publisher.SECONDARY_HISTORY_SQL:
            assert args[0] == [good['id']]
        return []

    connection = SimpleNamespace(fetch=fetch, fetchrow=AsyncMock(return_value={}),
                                 fetchval=AsyncMock(return_value='50'), close=AsyncMock())

    def transaction(**kwargs):
        assert kwargs == {'isolation': 'repeatable_read', 'readonly': True}
        return Transaction()

    connection.transaction = transaction
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr('asyncpg.connect', connect)
    monkeypatch.setattr(publisher, 'load_daily_picks', lambda: {'items': [{'id': relist_id}]})
    estimated = []

    def estimate(row, rentals, now):
        estimated.append(str(row['id']))
        return None

    monkeypatch.setattr(publisher, 'estimate_rental_offer', estimate)
    ps, ss, edge, coverage = await publisher.read_market_rows('postgresql://unused:unused@127.0.0.1:1/quarantine_test', NOW)
    assert [row['id'] for row in ss] == [good['id']]
    assert relist_id not in estimated and str(CONFLICT_ID) not in estimated
    assert connect.call_args.kwargs['server_settings']['default_transaction_read_only'] == 'on'
    assert all(sql.lstrip().upper().startswith(('SELECT', 'WITH')) for sql, args in calls)
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_truncated_quarantine_expansion_fails_closed_and_closes_reader(monkeypatch):
    monkeypatch.setattr(publisher, 'MAX_QUARANTINE_LINKS', 1)
    monkeypatch.setattr(publisher, 'load_daily_picks', lambda: None)

    async def fetch(sql, *args):
        if sql == publisher.QUARANTINED_LINKS_SQL:
            return [{'id': str(CONFLICT_ID)}, {'id': '999999'}]
        assert sql in {publisher.PRIMARY_SQL, publisher.SECONDARY_SQL}
        return []

    connection = SimpleNamespace(fetch=fetch, transaction=lambda **kwargs: Transaction(), close=AsyncMock())
    monkeypatch.setattr('asyncpg.connect', AsyncMock(return_value=connection))
    with pytest.raises(ValueError, match='Incomplete bounded source quarantine'):
        await publisher.read_market_rows('postgresql://unused:unused@127.0.0.1:1/quarantine_test', NOW)
    connection.close.assert_awaited_once()
