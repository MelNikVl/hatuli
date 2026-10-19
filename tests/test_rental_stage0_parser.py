"""Stage-0 parser tests with synthetic HTML and mocked PostgreSQL/HTTP only."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock
import inspect

import asyncpg
import httpx
import pytest
from bs4 import BeautifulSoup

from bot.core import rental_parser as parser, apartment_details, rental_stage0_identity as identity
from bot.core.rental_period import classify_rental_period, rental_period_conflict, catalog_period, catalog_cursor_key, catalog_page_url
from bot.db import pg, settings


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/rental_stage0_test")
    async def deny(*args, **kwargs):
        raise AssertionError("No real database or HTTP in stage-0 parser tests")
    monkeypatch.setattr(asyncpg, "connect", deny)
    monkeypatch.setattr(asyncpg, "create_pool", deny)
    monkeypatch.setattr(pg, "fetch", deny)
    monkeypatch.setattr(pg, "fetchrow", deny)
    monkeypatch.setattr(pg, "fetchval", deny)
    monkeypatch.setattr(pg, "execute", deny)


def card(price="200 000 ₸", title="2-комнатная, 60 м²"):
    return BeautifulSoup(f'<div class="a-card" data-id="123456"><a class="a-card__title" href="/a/show/123456">{title}</a><div class="a-card__price">{price}</div></div>', "html.parser").div


@pytest.mark.parametrize("price,title,expected", [
    ("15 000 ₸ за сутки", "Квартира", "daily"), ("15 000 ₸ / сутки", "Квартира", "daily"),
    ("15 000 ₸", "Квартира посуточно", "daily"), ("250 000 ₸ за месяц", "Квартира", "monthly"),
    ("250 000 ₸ / месяц", "Квартира", "monthly"), ("250 000 ₸ в месяц", "Квартира", "monthly"),
    ("250 000 ₸ / мес.", "Квартира", "monthly"), ("250 000 ₸", "Сдам помесячно", "monthly"),
    ("250 000 ₸ айына", "Квартира", "monthly"), ("20 000 ₸ тәулік", "Квартира", "daily"),
    ("20 000 ₸ per night", "Квартира", "daily"), ("250 000 ₸ per month", "Квартира", "monthly"),
    ("300 000 ₸", "Квартира", "unknown"), ("10 000 ₸", "Квартира", "unknown"),
    ("2 000 000 ₸", "Долгосрочно на 3 месяца", "unknown"), ("10 000 ₸ за 1 час", "Квартира", "unknown"),
    ("300 000 ₸ за месяц", "Квартира посуточно", "unknown"), ("300 000 ₸", "Не посуточно", "unknown"),
])
def test_period_is_explicit_text_never_price_or_contract_duration(price, title, expected):
    assert classify_rental_period(price, title) == expected
    assert parser._parse_card(card(price, title), "apartment").rent_period == expected


def test_verified_catalog_provenance_conflict_and_legacy_defaults():
    assert classify_rental_period("200 000 ₸", verified_catalog_period="monthly") == "monthly"
    assert classify_rental_period("20 000 ₸", verified_catalog_period="daily") == "daily"
    assert classify_rental_period("20 000 ₸ за сутки", verified_catalog_period="monthly") == "unknown"
    assert rental_period_conflict("20 000 ₸ за сутки", verified_catalog_period="monthly") is True
    assert rental_period_conflict("250 000 ₸ за месяц", verified_catalog_period="daily") is True
    assert rental_period_conflict("250 000 ₸", verified_catalog_period="monthly") is False
    assert classify_rental_period("20 000 ₸ за час", verified_catalog_period="monthly") == "unknown"
    assert parser._parse_card(card(), "apartment").rent_period == "unknown"
    assert parser.RentalListing.__dataclass_fields__["rent_period"].default == "unknown"


def test_verified_period_paths_have_separate_cursors_and_unknown_filter_is_not_invented():
    monthly, daily = "/arenda/kvartiry/astana/", "/arenda/kvartiry-posutochno/astana/"
    assert catalog_period(monthly) == "monthly" and catalog_period(daily) == "daily"
    assert catalog_period(monthly+"?page=2") == "monthly"
    assert catalog_period(monthly+"?das[rent_period]=daily") == "unknown"
    assert catalog_period("/arenda/kvartiry-po-chasam/astana/") == "unknown"
    assert catalog_period("https://evil.test"+monthly) == "unknown"
    assert catalog_cursor_key("apartment", monthly) == "RENTAL_CRAWL_V1_APARTMENT"
    assert catalog_cursor_key("apartment", daily) == "RENTAL_CRAWL_V1_APARTMENT_DAILY"
    assert catalog_cursor_key("apartment", monthly+"?unverified=1") != catalog_cursor_key("apartment", monthly)
    assert catalog_page_url(daily, 2) == "https://krisha.kz"+daily+"?page=2"
    assert "?q=verified&page=2" in catalog_page_url(monthly+"?q=verified", 2)
    with pytest.raises(ValueError):
        catalog_page_url("https://evil.test/private", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("path,expected", [("/arenda/kvartiry/astana/", "monthly"), ("/arenda/kvartiry-posutochno/astana/", "daily")])
async def test_real_fetch_parser_receives_verified_catalog_period(path, expected):
    html = '<div class="a-list a-search-list">'+str(card())+'</div>'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html))) as client:
        result = await parser._fetch_page_result(client, "https://krisha.kz"+path, "apartment")
    assert result.listings[0].rent_period == expected


@pytest.mark.asyncio
async def test_daily_and_monthly_progress_remain_separate_in_real_collector(monkeypatch):
    stored = {}
    monkeypatch.setattr(settings, "load", AsyncMock())
    monkeypatch.setattr(settings, "get", lambda key, default="": stored.get(key, default))
    async def remember(key, value):
        stored[key] = value
    monkeypatch.setattr(settings, "set", remember)
    save = AsyncMock(return_value=1)
    monkeypatch.setattr(parser, "save_rental_listings", save)
    html = '<div class="a-list a-search-list">'+str(card())+'</div>'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html))) as client:
        await parser.collect_rental_page(client, "/arenda/kvartiry/astana/", "apartment")
        await parser.collect_rental_page(client, "/arenda/kvartiry-posutochno/astana/", "apartment")
    assert parser.RentalPagination.loads(stored["RENTAL_CRAWL_V1_APARTMENT"]).page == 2
    assert parser.RentalPagination.loads(stored["RENTAL_CRAWL_V1_APARTMENT_DAILY"]).page == 2
    assert [call.args[0][0].rent_period for call in save.await_args_list] == ["monthly", "daily"]


@pytest.mark.asyncio
async def test_upsert_writes_period_and_id_and_gates_history_on_equal_known_period(monkeypatch):
    write = AsyncMock(return_value={"is_new": True, "reactivated": False})
    monkeypatch.setattr(pg, "fetchrow", write)
    resolver = AsyncMock(return_value=10)
    monkeypatch.setattr(parser, "resolve_rental_complex", resolver)
    listing = parser._parse_card(card("20 000 ₸ за сутки"), "apartment")
    assert await parser.save_rental_listings([listing]) == 1
    sql, *values = write.await_args.args
    assert values[-3:] == ["daily", 10, False]
    assert "rent_period, complex_id, hex_id, hex_edge_m" in sql
    assert "rental_listings.rent_period <> EXCLUDED.rent_period) THEN 'unknown'" in sql
    assert "rental_listings.rent_period_conflict OR EXCLUDED.rent_period_conflict" in sql
    assert "previous.rent_period = $18 AND $18 IN ('daily','monthly')" in sql
    assert "AND NOT previous.rent_period_conflict AND NOT $20" in sql
    assert "INSERT INTO rental_price_history" in sql and "CREATE " not in sql


@pytest.mark.asyncio
async def test_missing_migration_is_explicit_and_does_not_fall_back_to_legacy_write(monkeypatch):
    write = AsyncMock(side_effect=asyncpg.UndefinedColumnError('column "rent_period" does not exist'))
    monkeypatch.setattr(pg, "fetchrow", write)
    with pytest.raises(identity.RentalStage0MigrationRequired, match="109_rental_period_complex_hex"):
        await parser.save_rental_listings([parser._parse_card(card(), "apartment")])
    assert write.await_count == 1


@pytest.mark.asyncio
async def test_missing_backfill_migration_stops_before_source_or_write(monkeypatch):
    monkeypatch.setattr(apartment_details, "detail_fetch_cooldown_remaining", lambda: 0)
    monkeypatch.setattr(pg, "fetch", AsyncMock(side_effect=asyncpg.UndefinedColumnError("missing complex_id")))
    fetch = AsyncMock()
    write = AsyncMock()
    monkeypatch.setattr(apartment_details, "fetch_apartment_details", fetch)
    monkeypatch.setattr(pg, "execute", write)
    with pytest.raises(identity.RentalStage0MigrationRequired):
        await parser.backfill_rental_details(1)
    fetch.assert_not_awaited()
    write.assert_not_awaited()


@pytest.mark.parametrize("lat,lon,edge", [(float("nan"),71.4,100),(51.1,float("inf"),100),(51.1,71.4,0),(True,71.4,100),(51.1,71.4,True),(48,71.4,100),(51.1,71.4,"bad")])
def test_hex_requires_real_known_astana_coordinates_and_valid_setting(lat, lon, edge):
    assert identity.observed_hex(lat, lon, edge) is None


def test_hex_records_exact_configured_edge_not_a_guessed_complex_centroid():
    from bot.core.hexgrid import hex_id
    result = identity.observed_hex(51.128, 71.430, "100")
    assert result == {"lat":51.128,"lon":71.430,"hex_id":hex_id(51.128,71.430,100),"hex_edge_m":100.0}


@pytest.mark.asyncio
async def test_known_coordinates_receive_hex_without_detail_fetch(monkeypatch):
    from bot.core import complex_audit
    monkeypatch.setattr(apartment_details, "detail_fetch_cooldown_remaining", lambda: 0)
    monkeypatch.setattr(complex_audit, "street_names", AsyncMock(return_value=set()))
    monkeypatch.setattr(settings, "get", lambda key, default=None: "100")
    monkeypatch.setattr(pg, "fetch", AsyncMock(return_value=[{"id":"123456","url":"https://krisha.kz/a/show/123456", "lat":51.128,"lon":71.430,"complex_id":10,"complex_name":"Пример","complex_url":"https://krisha.kz/complex/show/10"}]))
    write, fetch = AsyncMock(), AsyncMock()
    monkeypatch.setattr(pg, "execute", write)
    monkeypatch.setattr(apartment_details, "fetch_apartment_details", fetch)
    await parser.backfill_rental_details(1)
    assert write.await_args.args[1:] == ("123456", "0:0", 100.0)
    fetch.assert_not_awaited()


@pytest.fixture
def resolver_catalog(monkeypatch):
    from bot.core import complex_ingest_identity as shared, house_resolution as houses
    monkeypatch.setattr(pg, "fetch", AsyncMock(return_value=[{"id":10}]))
    canonical = AsyncMock(side_effect=lambda cid: cid)
    monkeypatch.setattr(shared, "canonical_complex_id", canonical)
    monkeypatch.setattr(shared, "resolve_complex_name", AsyncMock(return_value=10))
    monkeypatch.setattr(pg, "fetchrow", AsyncMock(return_value={"id":10,"name":"Пример","parent_complex_id":None}))
    monkeypatch.setattr(houses, "get_umbrella_children", AsyncMock(return_value=[]))
    return shared, houses, canonical


@pytest.mark.asyncio
async def test_source_identity_uses_shared_canonical_resolver_and_no_name_only_or_geo(resolver_catalog):
    assert await identity.resolve_rental_complex(complex_url="https://krisha.kz/complex/show/10", complex_name="Пример") == 10
    resolver_catalog[2].assert_awaited_once_with(10)
    assert await identity.resolve_rental_complex(complex_url=None, complex_name="Пример", address="Пример 12") is None
    assert "listing_lat=None, listing_lon=None" in inspect.getsource(identity.resolve_rental_complex)


@pytest.mark.asyncio
async def test_conflicting_source_and_name_never_bind(resolver_catalog, monkeypatch):
    shared, _, _ = resolver_catalog
    monkeypatch.setattr(shared, "resolve_complex_name", AsyncMock(return_value=99))
    assert await identity.resolve_rental_complex(complex_url="https://krisha.kz/complex/show/10", complex_name="Другой") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("method,expected", [("address",11),("token",11),("geo",10),("address_geo",10),(None,10)])
async def test_umbrella_only_refines_to_a_child_with_explicit_address_or_token(resolver_catalog, monkeypatch, method, expected):
    _, houses, _ = resolver_catalog
    monkeypatch.setattr(houses, "get_umbrella_children", AsyncMock(return_value=[{"id":11,"name":"Пример 1"}]))
    resolved = AsyncMock(return_value={"house_id":11,"method":method} if method else None)
    monkeypatch.setattr(houses, "resolve_house", resolved)
    async def catalog(sql, cid):
        return {"id":10,"name":"Пример","parent_complex_id":None} if cid==10 else {"parent_complex_id":10}
    monkeypatch.setattr(pg, "fetchrow", catalog)
    assert await identity.resolve_rental_complex(complex_url="https://krisha.kz/complex/show/10", complex_name="Пример", address="Сыганак 10", title="Корпус 1") == expected
    assert resolved.await_args.kwargs["listing_lat"] is resolved.await_args.kwargs["listing_lon"] is None
