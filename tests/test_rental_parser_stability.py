"""Rental list throughput, honest sweep evidence and bounded detail enrichment."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.core import apartment_details, rental_parser as rentals
from bot.db import pg, settings

PATH = "/arenda/kvartiry/astana/"
KEY = "RENTAL_CRAWL_V1_APARTMENT"


def card(listing_id, *, price="200 000", extra="", has_id=True):
    ident = f'data-id="{listing_id}"' if has_id else ""
    return (f'<div class="a-card" {ident}><a class="a-card__title" '
            f'href="/a/show/{listing_id}">2-комнатная, 50 м²</a>'
            f'<div class="a-card__price">{price}</div>{extra}</div>')


def page(listing_id, *, last=5, total=100, current=1):
    return (f'<h1>Найдено {total} объявлений</h1>'
            f'<div class="a-list a-search-list">{card(listing_id)}</div>'
            '<nav class="paginator">'
            f'<a href="?page={current}">{current}</a>'
            f'<a href="?page={last}" rel="last">{last}</a></nav>')


@pytest.fixture
def durable_settings(monkeypatch):
    values = {}
    monkeypatch.setattr(settings, "load", AsyncMock())
    monkeypatch.setattr(settings, "get", lambda key, default="": values.get(key, default))

    async def save_state(key, value):
        values[key] = value

    monkeypatch.setattr(settings, "set", save_state)
    monkeypatch.setattr(rentals.asyncio, "sleep", AsyncMock())
    return values


@pytest.mark.asyncio
async def test_page_one_is_fetched_and_saved_once_when_head_refresh_enabled(monkeypatch, durable_settings):
    save = AsyncMock(return_value=rentals.RentalSaveCount(1, new=1))
    monkeypatch.setattr(rentals, "save_rental_listings", save)
    requested = []

    def serve(request):
        requested.append(request.url.params.get("page", "1"))
        return httpx.Response(200, text=page("101", last=2, total=2))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        result = await rentals.collect_rental_page(client, PATH, "apartment", refresh_head=True)
    assert requested == ["1"]
    assert save.await_count == 1
    assert result["new"] == result["requests"] == result["head_pages"] == result["durable_pages"] == 1
    assert not result["completed"]
    cursor = rentals.RentalPagination.loads(durable_settings[KEY])
    assert cursor.page == 2 and cursor.started_from_first and cursor.seen_ids == {"101"}


@pytest.mark.asyncio
async def test_full_sweep_completes_at_last_page_without_out_of_range_get(monkeypatch, durable_settings):
    monkeypatch.setattr(rentals, "save_rental_listings", AsyncMock(return_value=1))
    requested = []

    def serve(request):
        number = int(request.url.params.get("page", "1"))
        requested.append(number)
        return httpx.Response(200, text=page(str(number), last=2, total=2, current=number))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        await rentals.collect_rental_page(client, PATH, "apartment", refresh_head=True)
        durable_settings[f"{KEY}_STARTED_AT"] = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        result = await rentals.collect_rental_page(client, PATH, "apartment", refresh_head=True)
    assert requested == [1, 1, 2]
    assert result["completed"] and result["durable_pages"] == 1
    assert durable_settings[f"{KEY}_UNIQUE_SEEN"] == "2"
    assert durable_settings[f"{KEY}_LAST_PAGES"] == "2"
    assert durable_settings[f"{KEY}_TOTAL"] == "2"
    assert int(durable_settings[f"{KEY}_DURATION_SEC"]) >= 12 * 3600
    assert rentals.RentalPagination.loads(durable_settings[KEY]).page == 1
    pause = rentals.asyncio.sleep.await_args.args[0]
    assert rentals.MIN_SLEEP <= pause <= rentals.MAX_SLEEP


@pytest.mark.asyncio
async def test_old_tail_cannot_be_reported_as_a_complete_sweep(monkeypatch, durable_settings):
    durable_settings[KEY] = '{"page":6,"signatures":[]}'
    monkeypatch.setattr(rentals, "save_rental_listings", AsyncMock(return_value=1))
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=page("6", last=6, current=6)))) as client:
        result = await rentals.collect_rental_page(client, PATH, "apartment")
    assert result["round_ended"] and not result["completed"]
    assert f"{KEY}_COMPLETED_AT" not in durable_settings
    assert durable_settings[f"{KEY}_LAST_STATUS"] == "incomplete_legacy_tail"
    assert f"{KEY}_INCOMPLETE_COMPLETED_AT" in durable_settings


@pytest.mark.asyncio
async def test_verified_head_boundary_repairs_old_out_of_range_cursor_without_fake_completion(monkeypatch, durable_settings):
    durable_settings[KEY] = '{"page":203,"signatures":[]}'
    monkeypatch.setattr(rentals, "save_rental_listings", AsyncMock(return_value=1))
    requests = []

    def serve(request):
        requests.append(request.url)
        return httpx.Response(200, text=page("1", last=5))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        result = await rentals.collect_rental_page(client, PATH, "apartment", refresh_head=True)
    assert len(requests) == 1 and result["page"] == 1
    assert result["cursor_reconciled"] and not result["completed"]
    assert durable_settings[f"{KEY}_ABORT_REASON"] == "cursor_beyond_confirmed_catalog_end"
    assert f"{KEY}_COMPLETED_AT" not in durable_settings
    assert rentals.RentalPagination.loads(durable_settings[KEY]).page == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status,html,error", [
    (468, "blocked", httpx.HTTPStatusError),
    (200, "Подтвердите, что вы не робот. CAPTCHA", rentals.RentalSearchBlockedError),
    (200, '<div class="a-list a-search-list"></div>', ValueError),
])
async def test_failed_head_keeps_cursor_and_never_counts_completed_sweep(monkeypatch, durable_settings, status, html, error):
    original = '{"page":203,"signatures":[]}'
    durable_settings[KEY] = original
    save = AsyncMock()
    monkeypatch.setattr(rentals, "save_rental_listings", save)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text=html))) as client:
        with pytest.raises(error) as failure:
            await rentals.collect_rental_page(client, PATH, "apartment", refresh_head=True)
    assert durable_settings[KEY] == original
    assert failure.value.rental_metrics["requests"] == 1
    assert failure.value.rental_metrics["durable_pages"] == 0
    assert f"{KEY}_COMPLETED_AT" not in durable_settings
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_deep_block_keeps_successful_head_observations_in_metrics(monkeypatch, durable_settings):
    original = '{"page":6,"signatures":[]}'
    durable_settings[KEY] = original
    monkeypatch.setattr(rentals, "save_rental_listings", AsyncMock(
        return_value=rentals.RentalSaveCount(1, new=1)))

    def serve(request):
        if request.url.params.get("page") == "6":
            return httpx.Response(468, text="blocked")
        return httpx.Response(200, text=page("1", last=10))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        with pytest.raises(httpx.HTTPStatusError) as failure:
            await rentals.collect_rental_page(client, PATH, "apartment", refresh_head=True)
    assert failure.value.rental_metrics == {
        "requests": 2, "head_pages": 1, "durable_pages": 0,
        "saved": 1, "new": 1, "updated": 0, "reactivated": 0,
        "raw_cards": 1, "accepted_cards": 1, "unpriced_cards": 0,
    }
    assert durable_settings[KEY] == original


@pytest.mark.asyncio
async def test_main_inventory_excludes_promoted_and_footer_cards():
    html = ('<div class="highlighted-section">' + card("9") + '</div>' +
            '<div class="a-list a-search-list">' + card("1", has_id=False) +
            card("1") + '</div><footer>' + card("10") + '</footer>')
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=html))) as client:
        result = await rentals._fetch_page_result(client, rentals.BASE_URL + PATH, "apartment")
    assert set(result.card_ids) == {"1"}
    assert [listing.id for listing in result.listings] == ["1"]


@pytest.mark.asyncio
async def test_nested_promoted_section_is_not_counted_as_main_catalog_inventory():
    html = ('<div class="a-list a-search-list">' + card("1") +
            '<div class="highlighted-section">' + card("9") + '</div>' +
            card("2").replace('<div class="a-card"', '<section class="a-card"', 1).rsplit('</div>', 1)[0] +
            '</section></div>')
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=html))) as client:
        result = await rentals._fetch_page_result(client, rentals.BASE_URL + PATH, "apartment")
    assert result.card_ids == ("1", "2")
    assert [listing.id for listing in result.listings] == ["1", "2"]


@pytest.mark.asyncio
async def test_explicit_challenge_with_recommendation_cards_is_still_blocked():
    html = '<h1>Подтвердите, что вы не робот</h1><div class="highlighted-section">' + card("1") + '</div>'
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=html))) as client:
        with pytest.raises(rentals.RentalSearchBlockedError):
            await rentals._fetch_page_result(client, rentals.BASE_URL + PATH, "apartment")


@pytest.mark.asyncio
async def test_live_pagination_shape_proves_last_page_and_does_not_treat_next_window_as_end():
    html = card("1") + '<nav class="paginator">' + ''.join(
        f'<a href="?rent-period-switch=%2Farenda%2Fkvartiry&amp;page={n}">{n}</a>'
        for n in range(2, 9)) + '<span>...</span><a href="?page=207">207</a>' \
        '<a href="?page=2" class="paginator__btn--next">Дальше</a></nav>'
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=html))) as client:
        result = await rentals._fetch_page_result(client, rentals.BASE_URL + PATH, "apartment")
    assert result.last_page == 207 and result.last_page_confirmed and result.has_next


@pytest.mark.asyncio
async def test_detail_cooldown_does_not_block_list_page(monkeypatch, durable_settings):
    monkeypatch.setattr(apartment_details, "detail_fetch_cooldown_remaining", lambda: 900)
    monkeypatch.setattr(rentals, "save_rental_listings", AsyncMock(return_value=1))
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=page("1")))) as client:
        result = await rentals.collect_rental_page(client, PATH, "apartment", refresh_head=True)
    assert result["saved"] == 1 and result["durable_pages"] == 1


@pytest.mark.asyncio
async def test_list_save_is_atomic_with_history_and_issues_no_detail_http(monkeypatch):
    outcomes = [{"is_new": True, "reactivated": False},
                {"is_new": False, "reactivated": True}]
    write = AsyncMock(side_effect=outcomes)
    monkeypatch.setattr(pg, "fetchrow", write)
    detail = AsyncMock(side_effect=AssertionError("hot path must not fetch details"))
    monkeypatch.setattr(rentals, "fetch_complex_name", detail)
    items = [rentals._parse_card(rentals.BeautifulSoup(card(n), "html.parser").div, "apartment")
             for n in ("1", "2", "2")]
    result = await rentals.save_rental_listings(items)
    assert result == 2 and result.new == 1 and result.updated == 1 and result.reactivated == 1
    assert write.await_count == 2
    sql = write.await_args.args[0]
    assert "INSERT INTO rental_price_history" in sql and "INSERT INTO rental_listings" in sql
    assert "is_active = TRUE" in sql and "archived_at = NULL" in sql
    assert "CREATE TABLE" not in sql
    detail.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_row_is_not_counted_as_saved_or_new(monkeypatch):
    monkeypatch.setattr(pg, "fetchrow", AsyncMock(side_effect=RuntimeError("write failed")))
    listing = rentals._parse_card(rentals.BeautifulSoup(card("1"), "html.parser").div, "apartment")
    result = await rentals.save_rental_listings([listing])
    assert result == result.new == result.updated == 0


def test_official_complex_link_records_provenance_but_external_link_does_not():
    official = '<a href="/complex/show/55">ЖК Actual Complex</a>'
    external = '<a href="https://example.com/complex/show/55">Invented</a>'
    listing = rentals._parse_card(rentals.BeautifulSoup(card("1", extra=official), "html.parser").div, "apartment")
    assert listing.complex_name == "Actual Complex"
    assert listing.complex_url == "https://krisha.kz/complex/show/55"
    unverified = rentals._parse_card(rentals.BeautifulSoup(card("1", extra=external), "html.parser").div, "apartment")
    assert unverified.complex_url is None


@pytest.mark.parametrize("price,expected", [("5 570 000 ₸ / месяц", 5570000),
                                          ("28 000 000 ₸", 28000000),
                                          ("10 000 ₸ за 1 час", 10000),
                                          ("Договорная", None),
                                          ("2 147 483 648 ₸", None)])
def test_market_inventory_has_no_apartment_price_cap_and_does_not_join_period_digits(price, expected):
    listing = rentals._parse_card(rentals.BeautifulSoup(card("1", price=price), "html.parser").div, "commercial")
    assert listing is not None and listing.price == expected


@pytest.mark.parametrize("attribute", ["src", "data-src"])
def test_observed_primary_photo_is_collected_without_detail_fetch(attribute):
    photo = "https://krisha-photos.kcdn.online/webp/ab/12345678-1234-1234-1234-123456789abc/1-thumb.webp"
    listing = rentals._parse_card(rentals.BeautifulSoup(
        card("1", extra=f'<img class="a-image__img" {attribute}="{photo}">'), "html.parser").div, "apartment")
    assert listing.photo_url == photo


def test_arbitrary_image_host_is_not_persisted_as_source_photo():
    listing = rentals._parse_card(rentals.BeautifulSoup(
        card("1", extra='<img class="a-image__img" src="https://example.com/private.jpg">'), "html.parser").div, "apartment")
    assert listing.photo_url is None


def test_live_rental_preview_cdn_image_is_saved_without_weakening_daily_pick_rules():
    from bot.core.daily_picks import safe_photo_url
    photo = "https://astps-photos-kr.kcdn.kz/webp/56/56e36366-f634-480b-be27-f81efd89f4b1/4-400x300.jpg"
    listing = rentals._parse_card(rentals.BeautifulSoup(
        card("1", extra=f'<img class="a-image__img" src="{photo}">'), "html.parser").div, "apartment")
    assert listing.photo_url == photo
    assert safe_photo_url(photo) is None


@pytest.mark.parametrize("url", [
    "https://astps-photos-kr.kcdn.kz.evil.test/webp/56/56e36366-f634-480b-be27-f81efd89f4b1/4-400x300.jpg",
    "http://astps-photos-kr.kcdn.kz/webp/56/56e36366-f634-480b-be27-f81efd89f4b1/4-400x300.jpg",
    "https://user@astps-photos-kr.kcdn.kz/webp/56/56e36366-f634-480b-be27-f81efd89f4b1/4-400x300.jpg",
    "https://astps-photos-kr.kcdn.kz:443/webp/56/56e36366-f634-480b-be27-f81efd89f4b1/4-400x300.jpg",
    "https://astps-photos-kr.kcdn.kz/webp/56/56e36366-f634-480b-be27-f81efd89f4b1/4-400x300.jpg?token=secret",
    "https://astps-photos-kr.kcdn.kz/webp/56/56e36366-f634-480b-be27-f81efd89f4b1/4-400x300.jpg#fragment",
    "https://astps-photos-kr.kcdn.kz/private.jpg",
])
def test_rental_preview_validator_rejects_unapproved_urls(url):
    assert rentals._rental_card_photo_url(url) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["parking", "commercial"])
async def test_untyped_nonapartment_rent_units_are_not_used_as_monthly_estimates(monkeypatch, kind):
    read = AsyncMock(side_effect=AssertionError("unknown monthly unit"))
    monkeypatch.setattr(pg, "fetchrow", read)
    assert await rentals.lookup_rental_estimate("astana", None, None, None, kind) is None
    read.assert_not_awaited()


@pytest.fixture
def backfill_dependencies(monkeypatch):
    from bot.core import complex_audit
    rows = [{"id": "1", "url": "https://krisha.kz/a/show/1"},
            {"id": "2", "url": "https://krisha.kz/a/show/2"}]
    monkeypatch.setattr(pg, "fetch", AsyncMock(return_value=rows))
    write = AsyncMock()
    monkeypatch.setattr(pg, "execute", write)
    monkeypatch.setattr(complex_audit, "street_names", AsyncMock(return_value=set()))
    monkeypatch.setattr(apartment_details, "detail_fetch_cooldown_remaining", lambda: 0)
    monkeypatch.setattr(apartment_details, "REQUEST_COUNTS", {"detail": 0})
    monkeypatch.setattr(rentals.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(rentals.random, "uniform", lambda *_: 8)
    return write


@pytest.mark.asyncio
async def test_backfill_skips_all_work_during_detail_cooldown(monkeypatch, backfill_dependencies):
    monkeypatch.setattr(apartment_details, "detail_fetch_cooldown_remaining", lambda: 900)
    fetch_details = AsyncMock()
    monkeypatch.setattr(apartment_details, "fetch_apartment_details", fetch_details)
    report = await rentals.backfill_rental_details(2)
    assert report["cooldown"] and report["checked"] == report["detail_attempts"] == 0
    pg.fetch.assert_not_awaited()
    backfill_dependencies.assert_not_awaited()
    fetch_details.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_stops_on_468_without_marking_unfetched_candidates(monkeypatch, backfill_dependencies):
    async def blocked(*args, **kwargs):
        apartment_details.REQUEST_COUNTS["detail"] += 1
        raise apartment_details.ListingBlockedError("468 https://krisha.kz/a/show/1")

    fetch_details = AsyncMock(side_effect=blocked)
    monkeypatch.setattr(apartment_details, "fetch_apartment_details", fetch_details)
    progress = {}
    report = await rentals.backfill_rental_details(2, budget_seconds=30, progress=progress)
    assert report is progress
    assert report["blocked"] == report["http_468"] == report["requests"] == 1
    assert report["checked"] == 0 and fetch_details.await_count == 1
    backfill_dependencies.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_budget_does_not_sleep_then_skip_requests(monkeypatch, backfill_dependencies):
    fetch_details = AsyncMock()
    monkeypatch.setattr(apartment_details, "fetch_apartment_details", fetch_details)
    report = await rentals.backfill_rental_details(2, budget_seconds=10)
    assert report["budget_exhausted"] and report["detail_attempts"] == 0
    fetch_details.assert_not_awaited()
    rentals.asyncio.sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_preserves_progress_if_parent_budget_cancels(monkeypatch, backfill_dependencies):
    async def canceled(*args, **kwargs):
        apartment_details.REQUEST_COUNTS["detail"] += 1
        raise rentals.asyncio.CancelledError()

    monkeypatch.setattr(apartment_details, "fetch_apartment_details", canceled)
    progress = {}
    with pytest.raises(rentals.asyncio.CancelledError):
        await rentals.backfill_rental_details(2, budget_seconds=30, progress=progress)
    assert progress["requests"] == progress["detail_attempts"] == 1
    assert progress["checked"] == 0
    backfill_dependencies.assert_not_awaited()


class Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_index_counts_unknown_complex_once_and_replaces_obsolete_groups(monkeypatch):
    from types import SimpleNamespace
    rows = [{"id": str(i), "city": "astana", "district": "", "complex_name": "Guessed",
             "complex_url": "https://krisha.kz/complex/show/astana/another-project",
             "rooms": 2, "prop_type": "apartment", "price": 200000 + i * 10000,
             "area": 50, "title": "2-комнатная, 50 м²"} for i in range(5)]
    rows += [{**rows[0], "title": "Посуточно, 2-комнатная"}]
    read = AsyncMock(side_effect=[rows, []])
    monkeypatch.setattr(pg, "fetch", read)
    connection = SimpleNamespace(execute=AsyncMock(), executemany=AsyncMock(),
                                 transaction=lambda: Context(None))
    monkeypatch.setattr(pg, "get_pool", lambda: SimpleNamespace(acquire=lambda: Context(connection)))
    await rentals.rebuild_rental_index()
    sql = read.await_args_list[0].args[0]
    assert "'72 hours'" in sql and "is_active IS TRUE" in sql
    assert "is_duplicate" in sql and "dup_needs_review" in sql
    assert "prop_type = 'apartment'" in sql
    connection.execute.assert_awaited_once_with("DELETE FROM rental_index")
    records = connection.executemany.await_args.args[1]
    assert len(records) == 2
    assert all(record[2] is None and record[9] == 5 for record in records)
    assert all(record[5] == 220000 for record in records)


@pytest.mark.asyncio
@pytest.mark.parametrize("verified_count", [4, 5])
async def test_index_complex_group_requires_enough_source_identity_verified_rows(monkeypatch, verified_count):
    from types import SimpleNamespace
    rows = [{"id": str(i), "city": "astana", "district": "", "complex_name": "Actual Complex",
             "complex_url": "https://krisha.kz/complex/show/astana/actual-complex",
             "rooms": 2, "prop_type": "apartment", "price": 220000,
             "area": 50, "title": "2-комнатная, 50 м²"} for i in range(5)]
    verified = [{"id": str(i), "complex_binding_verified": True} for i in range(verified_count)]
    monkeypatch.setattr(pg, "fetch", AsyncMock(side_effect=[rows, verified]))
    connection = SimpleNamespace(execute=AsyncMock(), executemany=AsyncMock(),
                                 transaction=lambda: Context(None))
    monkeypatch.setattr(pg, "get_pool", lambda: SimpleNamespace(acquire=lambda: Context(connection)))
    await rentals.rebuild_rental_index()
    records = connection.executemany.await_args.args[1]
    complex_groups = [record for record in records if record[2] == "Actual Complex"]
    assert len(complex_groups) == (2 if verified_count == 5 else 0)
    assert all(record[9] == 5 for record in records)


@pytest.mark.asyncio
async def test_index_removes_stale_groups_when_current_sample_insufficient(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(pg, "fetch", AsyncMock(return_value=[]))
    connection = SimpleNamespace(execute=AsyncMock(), executemany=AsyncMock(),
                                 transaction=lambda: Context(None))
    monkeypatch.setattr(pg, "get_pool", lambda: SimpleNamespace(acquire=lambda: Context(connection)))
    await rentals.rebuild_rental_index()
    connection.execute.assert_awaited_once_with("DELETE FROM rental_index")
    connection.executemany.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookup_refuses_stale_and_small_aggregate_via_database_predicate(monkeypatch):
    read = AsyncMock(return_value=None)
    monkeypatch.setattr(pg, "fetchrow", read)
    assert await rentals.lookup_rental_estimate("astana", "Есиль", "Example", 2) is None
    assert all("sample_count >= 5" in call.args[0] and "'72 hours'" in call.args[0]
               for call in read.await_args_list)


def test_renamed_catalog_paths_are_preserved():
    assert rentals.RENTAL_PATHS == {
        "/arenda/kvartiry/astana/": "apartment",
        "/arenda/garazhi/astana/": "parking",
        "/arenda/kommercheskaya-nedvizhimost/astana/": "commercial",
    }
