"""Exercise pagination/requests with synthetic HTTP and durable cursor state."""
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest

from bot.core import rental_parser as rentals
from bot.core import apartment_parser as sales


def card(lid, price="200 000"):
    return (f'<div class="a-card" data-id="{lid}"><a class="a-card__title" '
            f'href="/a/show/{lid}">2-комнатная, 50 м²</a>'
            f'<div class="a-card__price">{price}</div></div>')


@pytest.mark.asyncio
async def test_unbounded_rental_scan_passes_page_10_and_stops_on_repeated_last_page(monkeypatch):
    pages = []
    def serve(request):
        page = int(request.url.params.get("page", "1"))
        pages.append(page)
        return httpx.Response(200, text=card(str(min(page, 12))))
    client_class = httpx.AsyncClient
    monkeypatch.setattr(rentals.httpx, "AsyncClient", lambda **kw: client_class(
        transport=httpx.MockTransport(serve), **kw))
    monkeypatch.setattr(rentals.asyncio, "sleep", AsyncMock())
    result = await rentals.parse_rental_path("/arenda/kvartiry/astana/", "apartment")
    assert pages == list(range(1, 14))
    assert {r.id for r in result} == {str(i) for i in range(1, 13)}


@pytest.mark.asyncio
async def test_filtered_out_cards_are_not_end_of_search():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=card("1", "цена не указана")))) as client:
        page = await rentals._fetch_page_result(client, "https://krisha.kz/test", "apartment")
    assert page.listings == []
    cursor = rentals.RentalPagination()
    assert not cursor.advance(page)
    assert cursor.page == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status,html", [(503, "down"), (200, "Just a moment CAPTCHA"),
                                         (200, "<html>upstream error</html>")])
async def test_failed_search_is_not_empty_page(status, html):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text=html))) as client:
        with pytest.raises((httpx.HTTPStatusError, ValueError)):
            await rentals._fetch_page(client, "https://krisha.kz/test", "apartment")


@pytest.mark.asyncio
async def test_recognised_empty_search_completes_pass():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text='<div class="a-list"></div>'))) as client:
        page = await rentals._fetch_page_result(client, "https://krisha.kz/test", "apartment")
    cursor = rentals.RentalPagination(page=50)
    assert cursor.advance(page)
    assert cursor.page == 1


def test_cursor_roundtrip_preserves_repeat_detection():
    cursor = rentals.RentalPagination(page=17)
    assert not cursor.advance(rentals.RentalPage([], ("1", "2")))
    restored = rentals.RentalPagination.loads(cursor.dumps())
    assert restored.page == 18
    assert restored.advance(rentals.RentalPage([], ("2", "1")))
    assert restored.page == 1


@pytest.mark.asyncio
async def test_durable_page_step_resumes_and_does_not_advance_on_save_failure(monkeypatch):
    from bot.db import settings
    values = {"RENTAL_CRAWL_V1_APARTMENT": rentals.RentalPagination(page=6).dumps()}
    monkeypatch.setattr(settings, "load", AsyncMock())
    monkeypatch.setattr(settings, "get", lambda key, default="": values.get(key, default))
    async def save_state(key, value):
        values[key] = value
    monkeypatch.setattr(settings, "set", save_state)
    requested = []
    def serve(request):
        requested.append(request.url.params.get("page"))
        return httpx.Response(200, text=card("600"))
    save = AsyncMock(side_effect=RuntimeError("DB write failed"))
    monkeypatch.setattr(rentals, "save_rental_listings", save)
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        with pytest.raises(RuntimeError):
            await rentals.collect_rental_page(client, "/arenda/kvartiry/astana/", "apartment")
        assert rentals.RentalPagination.loads(values["RENTAL_CRAWL_V1_APARTMENT"]).page == 6
        save.side_effect = None
        save.return_value = 1
        result = await rentals.collect_rental_page(client, "/arenda/kvartiry/astana/", "apartment")
    assert requested == ["6", "6"]
    assert result["saved"] == 1 and not result["completed"]
    assert rentals.RentalPagination.loads(values["RENTAL_CRAWL_V1_APARTMENT"]).page == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_cap", [None, 0, 150_000_000])
async def test_sale_request_has_no_implicit_price_or_photo_filter(monkeypatch, explicit_cap):
    urls = []
    def serve(request):
        urls.append(request.url)
        return httpx.Response(200, text='<div class="a-list"></div>')
    client_class = httpx.AsyncClient
    monkeypatch.setattr(sales.httpx, "AsyncClient", lambda **kw: client_class(
        transport=httpx.MockTransport(serve), **kw))
    monkeypatch.setattr(sales.asyncio, "sleep", AsyncMock())
    kwargs = {} if explicit_cap is None else {"max_price": explicit_cap}
    await sales.parse_apartments_for_sale(max_pages=1, **kwargs)
    params = parse_qs(urls[0].query.decode())
    assert "das[_sys.hasphoto]" not in params
    if explicit_cap:
        assert params["das[price][to]"] == [str(explicit_cap)]
    else:
        assert "das[price][to]" not in params
