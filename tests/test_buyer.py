"""Buyer decisions, pairwise guarantees, ingestion, persistence and Telegram UX."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from bot.core import buyer, buyer_store


def listing(**overrides):
    d = dict(id='123456', price=32_000_000, area=52., rooms=2, lat=51.13, lon=71.43,
             floor=3, floors_total=12, is_active=True, market='secondary',
             deal_score={'deal': 76, 'confidence': 70},
             bargain={'median_price': 33_000_000, 'target_price': 31_000_000, 'comparables_cnt': 12, 'method': 'hex'},
             risk_analysis={'overall_level': 'info', 'items': []})
    d.update(overrides)
    return d


@pytest.mark.parametrize('text', [
    'https://krisha.kz/a/show/123456', 'Смотри https://krisha.kz/a/show/123456/?x=foo.',
    '(https://www.krisha.kz/a/show/123456)', 'www.krisha.kz/a/show/123456',
])
def test_extract(text):
    assert buyer.extract_krisha_url(text) == 'https://krisha.kz/a/show/123456'


@pytest.mark.parametrize('text', [
    'https://evil.kz/a/show/123456', 'https://krisha.kz.evil.com/a/show/123456',
    'https://krisha.kz@evil.com/a/show/123456', 'https://evil.com@krisha.kz/a/show/123456',
    'https://krisha.kz:999/a/show/123456', 'https://krisha.kz/a/show/123456/evil',
    'https://krisha.kz/a/show/12', '123456', 'https://krisha.kz/arenda/123456',
])
def test_extract_rejects(text):
    assert buyer.extract_krisha_url(text) is None


@pytest.mark.parametrize(('changes', 'code'), [
    ({}, 'view'), ({'price': 40_000_000}, 'negotiate'),
    ({'risk_analysis': {'overall_level': 'high', 'items': [{'title': 'Риск договора', 'severity': 'high'}]}}, 'skip'),
    ({'is_active': False}, 'skip'),
    ({'deal_score': None, 'bargain': {}}, 'unknown'),
    ({'price': 30_000_000, 'dom_scenario': {'available': True, 'confidence': 'high',
        'current': {'days_high': 20}}}, 'fast'),
])
def test_verdict(changes, code):
    r = buyer.summarize(listing(**changes))
    assert r['verdict']['code'] == code
    assert 2 <= len(r['verdict']['reasons']) <= 3
    assert len(r['positives']) <= 3 and len(r['negatives']) <= 3


def test_unknown_is_not_negative():
    r = buyer.summarize({'id': '123456'})
    assert r['score'] is None and r['negatives'] == []
    assert r['verdict']['code'] == 'unknown'
    assert r['urgency']['level'] == 'unknown'
    assert r['price_summary']['fair'] is None
    assert r['risks'] == []


def test_sparse_price_and_low_confidence_never_fast():
    d = listing(price=20_000_000, bargain={'median_price': 40_000_000, 'comparables_cnt': 2},
                deal_score={'deal': 95, 'confidence': 20},
                dom_scenario={'available': True, 'confidence': 'low', 'current': {'days_high': 5}})
    r = buyer.summarize(d)
    assert r['score'] is None and r['price_summary']['offer'] is None
    assert r['verdict']['code'] == 'unknown'
    assert r['urgency']['level'] == 'unknown'


def test_specific_checks_not_generic_questions():
    d = listing(floor=12, renovation='rough', risk_analysis={'overall_level': 'medium', 'items': [
        {'severity': 'medium', 'title': 'Шум', 'recommendation': 'Послушайте шум дороги у окна.'}]})
    r = buyer.summarize(d)
    assert len(r['risks']) == 3
    assert r['risks'][0] == 'Послушайте шум дороги у окна.'


def test_cheaper_larger_really_better():
    base = listing()
    c = listing(id='123457', price=30_900_000, area=56, lat=51.131)
    r = buyer.rank_alternatives(base, [c])[0]
    assert r['rank_points'] >= 3
    assert r['distance_m'] <= 500
    assert any('Дешевле' in t for t in r['advantages'])
    assert any('Больше' in t for t in r['advantages'])


@pytest.mark.parametrize('changes', [
    {}, {'price': 34_000_000, 'area': 50}, {'is_active': False}, {'is_active': None},
    {'is_duplicate': True}, {'lat': 51.3}, {'rooms': 1}, {'market': 'primary'},
    {'risk_analysis': {'overall_level': 'high'}}, {'price': None}, {'area': 0},
])
def test_similar_does_not_mean_better(changes):
    assert buyer.rank_alternatives(listing(), [listing(id='123457', **changes)]) == []


def test_missing_fields_do_not_create_an_advantage():
    base = listing(floor=1, ceiling_height=2.5, renovation='rough')
    c = listing(id='123457', floor=None, ceiling_height=None, renovation=None)
    assert buyer.compare_alternative(base, c) is None
    c['price'] = 30_000_000
    result = buyer.compare_alternative(base, c)
    assert result and result['warnings']
    assert not any('Этаж' in a or 'Потолки' in a or 'отделка' in a for a in result['advantages'])


def test_tradeoffs_and_property_dedup():
    base = listing(property_id=1)
    candidates = [listing(id=str(200000 + n), property_id=n // 2 + 2, price=29_000_000,
                          floor=1, area=52) for n in range(10)]
    candidates.append(listing(id='999999', property_id=1, price=20_000_000))
    results = buyer.rank_alternatives(base, candidates)
    assert len(results) == 3
    assert len({r['listing']['property_id'] for r in results}) == 3
    assert all('Этаж менее удобный' in r['tradeoffs'] for r in results)
    assert all(r['listing']['property_id'] != 1 for r in results)


def test_optional_profile_changes_decision_and_ranking():
    profile = dict(budget_max=30_000_000, rooms=[2, 4], area_min=50, property_type='secondary')
    base = listing()
    assert buyer.summarize(base)['verdict']['code'] == 'view'
    assert buyer.summarize(base, profile)['verdict']['code'] == 'skip'
    candidates = [listing(id='123457', price=31_000_000, area=58), listing(id='123458', price=29_000_000)]
    assert [r['listing']['id'] for r in buyer.rank_alternatives(base, candidates, profile)] == ['123458']
    assert buyer.profile_mismatches(listing(rooms=5, price=29_000_000), profile) == []


@pytest.mark.asyncio
async def test_unknown_listing_honest_failure():
    with patch.object(buyer, 'resolve_listing', AsyncMock(return_value=SimpleNamespace(data={'listing_id': '123456', 'found': False}))), \
         patch.object(buyer, 'import_listing', AsyncMock(return_value=False)) as imp:
        result = await buyer.analyze_for_buyer('https://krisha.kz/a/show/123456')
    assert result['found'] is False
    imp.assert_awaited_once_with('123456')


@pytest.mark.asyncio
async def test_existing_listing_does_not_scrape_and_reuses_candidates():
    d = listing(similar=[{'id': '123457', 'price': 29_000_000, 'area': 55}])
    c = listing(id='123457', price=29_000_000, area=55)
    with patch.object(buyer, 'resolve_listing', AsyncMock(return_value=SimpleNamespace(data={'found': True, 'listing_id': '123456'}))), \
         patch.object(buyer, 'import_listing', AsyncMock()) as imp, \
         patch.object(buyer, '_detail', AsyncMock(side_effect=[d, c])) as detail, \
         patch.object(buyer, 'compute_dom_scenario_cached', AsyncMock(return_value={})), \
         patch.object(buyer, 'get_profile', AsyncMock(return_value={})) as profile:
        result = await buyer.analyze_for_buyer('123456')
    assert result['found'] and len(result['better_nearby']) == 1
    imp.assert_not_called()
    profile.assert_not_called()
    assert detail.await_args_list[0].kwargs == {'similar_limit': 40}


@pytest.mark.asyncio
async def test_alternative_failure_degrades_gracefully():
    with patch.object(buyer, 'resolve_listing', AsyncMock(return_value=SimpleNamespace(data={'found': True, 'listing_id': '123456'}))), \
         patch.object(buyer, '_detail', AsyncMock(side_effect=[listing(similar=[{'id': '123457'}]), RuntimeError()])), \
         patch.object(buyer, 'compute_dom_scenario_cached', AsyncMock(side_effect=TimeoutError)):
        result = await buyer.analyze_for_buyer('123456')
    assert result['found'] and result['better_nearby'] == []
    assert any('не удалось' in w for w in result['warnings'])


@pytest.mark.asyncio
async def test_import_only_verified_scope_and_no_overwrite(monkeypatch):
    from bot.core import apartment_details
    monkeypatch.setattr(buyer, '_next_fetch', 0)
    details = {'listing_identity': {'sale_astana': True, 'rooms': 2, 'price': 32_000_000, 'area': 52}}
    with patch.object(apartment_details, 'fetch_apartment_details', AsyncMock(return_value=details)) as fetch, \
         patch.object(buyer.pg, 'execute', AsyncMock()) as execute:
        assert await buyer.import_listing('123456')
        assert 'ON CONFLICT (id) DO NOTHING' in execute.await_args.args[0]
        fetch.assert_awaited_once_with('https://krisha.kz/a/show/123456')
        assert not await buyer.import_listing('234567')  # global source cooldown


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [{'is_archived': True}, {'is_flat_layout': True}, {'is_price_from': True}, {'listing_identity': {}}, {'listing_identity': {'sale_astana': False}}])
async def test_import_rejects_unverifiable_or_archived(monkeypatch, changes):
    from bot.core import apartment_details
    monkeypatch.setattr(buyer, '_next_fetch', 0)
    details = {'listing_identity': {'sale_astana': True, 'rooms': 2, 'price': 32_000_000, 'area': 52}, **changes}
    with patch.object(apartment_details, 'fetch_apartment_details', AsyncMock(return_value=details)), \
         patch.object(buyer.pg, 'execute', AsyncMock()) as execute:
        assert not await buyer.import_listing('123456')
        execute.assert_not_called()


@pytest.mark.asyncio
async def test_existing_favorites_integration_idempotent():
    from bot.core import site_auth
    with patch.object(buyer_store.pg, 'fetchrow', AsyncMock(return_value={'id': '123456'})), \
         patch.object(buyer_store.pg, 'execute', AsyncMock()) as users, \
         patch.object(site_auth, 'execute', AsyncMock()) as favorites:
        assert await buyer_store.save_favorite(42, '123456')
    assert 'INSERT INTO users' in users.await_args.args[0]
    assert "'off'" in users.await_args.args[0]
    assert 'INSERT INTO favorites' in favorites.await_args.args[0]
    assert 'DO NOTHING' in favorites.await_args.args[0]
    assert favorites.await_args.args[1:] == (42, '123456')


@pytest.mark.asyncio
async def test_profile_persistence_existing_columns_only():
    p = dict(budget_max=35_000_000, rooms=[2, 4], area_min=50, property_type='new')
    with patch.object(buyer_store.pg, 'execute', AsyncMock()) as execute:
        await buyer_store.save_profile(42, p)
    sql, uid, budget, rooms, area, kind = execute.await_args.args
    assert (uid, budget, json.loads(rooms), area, kind) == (42, 35_000_000, [2, 4], 50, 'new')
    assert 'INSERT INTO users' in sql and 'CREATE' not in sql
    # Existing notification consent is never changed on conflict.
    assert 'notify_frequency' not in sql.split('DO UPDATE')[1]


@pytest.mark.parametrize('changes', [{'budget_max': -1}, {'budget_max': 3_000_000_000}, {'rooms': []}, {'rooms': [0]}, {'area_min': float('nan')}, {'property_type': 'evil'}])
def test_profile_validation(changes):
    with pytest.raises(ValueError):
        buyer_store.validate_profile({**dict(budget_max=30_000_000, rooms=[2]), **changes})


@pytest.mark.asyncio
@pytest.mark.parametrize(('href', 'expected'), [('/prodazha/kvartiry/astana/', True),
    ('/arenda/kvartiry/astana/', False), ('/prodazha/kvartiry/almaty/', False)])
async def test_existing_detail_parser_identity_scope(href, expected):
    from tests.test_apartment_details_floor_parsing import _fetch_with_html
    html = f'''<h1>2-комнатная квартира, 52 м²</h1><div class="offer__price">32 000 000 ₸</div>
        <div class="breadcrumbs"><a href="{href}">Квартиры</a></div>'''
    d = await _fetch_with_html(html)
    assert d['listing_identity'] == dict(sale_astana=expected, price=32_000_000, rooms=2, area=52.)


@pytest.mark.asyncio
async def test_recommended_link_not_scope_evidence():
    from tests.test_apartment_details_floor_parsing import _fetch_with_html
    d = await _fetch_with_html('<a href="/prodazha/kvartiry/astana/">Рекомендации</a><h1>Квартира</h1>')
    assert not d['listing_identity']['sale_astana']


@pytest.mark.parametrize('method', ['city_segment', 'district_fallback'])
def test_broad_price_sample_not_reliable(method):
    r = buyer.summarize(listing(bargain={'median_price': 45_000_000, 'comparables_cnt': 30, 'method': method}))
    assert r['price_summary']['fair'] is None


def test_legacy_location_not_used_as_amenity_score():
    d = listing(hex_details={'components': {'location': {'score': 100, 'text': 'дорогая локация'}}})
    assert buyer._location(d) is None
    d['location_score'] = {'score': 85, 'confidence': 70}
    assert buyer._location(d) == 85


def test_malformed_url_cannot_break_filter():
    assert buyer.extract_krisha_url('https://[invalid/a/show/123456') is None
