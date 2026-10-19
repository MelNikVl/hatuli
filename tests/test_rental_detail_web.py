"""Rental detail is private, rental-only, and preserves observed price events."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from bot import rental_detail_web as detail

NOW = datetime.now(timezone.utc)
COVER = 'https://astps-photos-kr.kcdn.kz/webp/56/56e36366-f634-480b-be27-f81efd89f4b1/4-400x300.jpg'


def rental(**changes):
    return {'id':'1234567','title':'2-комнатная квартира','prop_type':'apartment','rent_period':'monthly',
            'price':None,'area':60,'rooms':2,'floor':3,'floors_total':10,
            'address':'Адрес из источника','complex_name':'QA','last_seen':NOW-timedelta(minutes=4),
            'found_at':NOW-timedelta(days=4),'is_active':True,'archived_at':None,
            'photos':None,'photo_url':COVER,'phone':'private-contact','description':'private-description',
            'yield_pct':99,'est_rent':900000,'url':'javascript:private-url',**changes}


def test_projection_uses_cover_known_fields_and_preserves_actual_history_without_income():
    history=[{'old_price':300000,'new_price':250000,'changed_at':NOW-timedelta(days=1)}]
    data=detail.project_rental_detail(rental(),history,now=NOW)
    assert data['price'] is None and data['photos']==[COVER]
    assert data['source_url']=='https://krisha.kz/a/show/1234567'
    assert data['status']=='active_observed' and data['monthly_category']
    assert data['price_history'][0]['old_price']==300000 and data['price_history'][0]['new_price']==250000
    assert 'не подтверждает сдачу' in data['archive_note']
    serialized=json.dumps(data)
    for forbidden in ('private-contact','private-description','private-url','yield_pct','est_rent'):
        assert forbidden not in serialized


@pytest.mark.parametrize('changes,status',[
    ({'is_active':False,'archive_reason':'archived_badge'},'confirmed_removed'),
    ({'is_active':False,'archive_reason':'sweep_stale'},'unconfirmed_inactive'),
    ({'last_seen':NOW-timedelta(days=4)},'unknown'),
    ({'last_seen':NOW+timedelta(hours=1)},'unknown'),
    ({'is_active':None},'unknown'),
])
def test_archive_or_unknown_freshness_never_claims_rented(changes,status):
    data=detail.project_rental_detail(rental(**changes),[],now=NOW)
    assert data['status']==status and 'сдано' not in data['status_label'].lower()


def test_photos_and_partial_history_fail_closed_without_fabricating_first_price():
    data=detail.project_rental_detail(rental(photos=json.dumps(['javascript:bad','https://evil.test/x.jpg',COVER])),
        [{'old_price':None,'new_price':250000,'changed_at':NOW}]*101,now=NOW)
    assert data['photos']==[COVER] and data['price_history'][0]['old_price'] is None
    assert len(data['price_history'])==100 and data['history_truncated']


def client(monkeypatch,role='admin',*,missing=False,history_failure=False):
    monkeypatch.setattr(detail,'get_user_tier',AsyncMock(return_value=role))
    row=AsyncMock(return_value=None if missing else rental())
    history=AsyncMock(return_value=[{'old_price':300000,'new_price':250000,'changed_at':NOW}],
                      side_effect=RuntimeError('private-db-secret') if history_failure else None)
    monkeypatch.setattr(detail,'fetchrow',row)
    monkeypatch.setattr(detail,'fetch',history)
    app=FastAPI()
    detail.register_rental_detail_routes(app)
    return TestClient(app),row,history


@pytest.mark.parametrize('role',['public',None,'unknown'])
def test_public_or_unknown_tier_cannot_read_rental_detail(monkeypatch,role):
    connection,row,history=client(monkeypatch,role)
    response=connection.get('/admin/api/rental/1234567')
    assert response.status_code==403 and response.headers['cache-control']=='no-store'
    row.assert_not_awaited(); history.assert_not_awaited()


@pytest.mark.parametrize('role',['admin','subscriber'])
def test_authenticated_flow_never_queries_sale_with_same_id(monkeypatch,role):
    connection,row,history=client(monkeypatch,role)
    response=connection.get('/admin/api/rental/1234567')
    assert response.status_code==200 and response.json()['price'] is None
    assert row.await_args.args==(detail.DETAIL_SQL,'1234567')
    assert 'FROM rental_listings WHERE id = $1' in row.await_args.args[0]
    assert 'apartment_listings' not in row.await_args.args[0]
    assert history.await_args.args==(detail.HISTORY_SQL,'1234567')
    assert response.headers['cache-control']=='no-store' and 'noindex' in response.headers['x-robots-tag']


def test_missing_rental_never_uses_existing_sale_as_fallback(monkeypatch):
    connection,row,history=client(monkeypatch,missing=True)
    response=connection.get('/admin/api/rental/1234567')
    assert response.status_code==404
    row.assert_awaited_once(); history.assert_not_awaited()


def test_history_failure_keeps_detail_available_and_does_not_claim_no_changes(monkeypatch):
    connection,row,history=client(monkeypatch,history_failure=True)
    response=connection.get('/admin/api/rental/1234567')
    assert response.status_code==200 and not response.json()['history_available']
    assert 'private-db-secret' not in response.text


@pytest.mark.parametrize('identifier',['bad','123','123456789012345678901'])
def test_invalid_ids_are_rejected_before_database_query(monkeypatch,identifier):
    connection,row,history=client(monkeypatch)
    response=connection.get('/admin/api/rental/'+identifier)
    assert response.status_code==422
    row.assert_not_awaited(); history.assert_not_awaited()
