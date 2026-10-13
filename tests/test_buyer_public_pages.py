"""Public snapshot renders without consulting private listings or exposing IDs."""
import copy
import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from bot.admin_web import create_admin_app
from bot.core.public_buyer_example import SNAPSHOT_PATH, build_public_example


@pytest.fixture(autouse=True)
def no_database_connections(monkeypatch):
    async def reject_connection(*args, **kwargs):
        raise AssertionError('Public page tests must not connect to PostgreSQL')

    monkeypatch.setattr('asyncpg.connect', reject_connection)
    monkeypatch.setattr('asyncpg.create_pool', reject_connection)


class PrivateDBMustNotBeRead:
    def __getattr__(self, name):
        raise AssertionError(f"Public example attempted private DB access: {name}")


@pytest.mark.asyncio
async def test_anonymous_marketing_pages_are_self_contained_and_work_with_forged_cookie():
    app = create_admin_app(PrivateDBMustNotBeRead(), "isolated-form-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", cookies={"admin_auth": "1"}) as client:
        for path in ("/", "/example", "/how-it-works", "/request-analysis", "/guides/check-apartment-before-deposit"):
            page = await client.get(path)
            assert page.status_code == 200, path
            assert "Hatuli" in page.text
            assert "Clearly" not in page.text
            assert "mc.yandex.ru" not in page.text
            assert "admin/analysis-requests" not in page.text
            assert 'href="None"' not in page.text
            assert 'href="/map"' in page.text
        report = (await client.get("/example")).text
        assert "Нужно уточнить" in report
        assert "33,25 млн" in report and "всем 30 объявлениям" in report
        assert "4 октября 2026 года" in report
        assert "Снятие объявления не означает продажу" in report
        assert 'stroke-dasharray="5 5"' in report
        assert report.count("Аналог ") == 5
        assert "/a/show/" not in report and "property_id" not in report
        form = await client.get("/request-analysis")
        assert form.headers["cache-control"] == "no-store"
        assert "HttpOnly" in form.headers["set-cookie"]


class PublicPageHead(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.meta = {}
        self.canonical = None
        self.share_href = None
        self.copy_url = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            key = attrs.get('property') or attrs.get('name')
            if key:
                assert key not in self.meta, f'duplicate metadata: {key}'
                self.meta[key] = attrs.get('content', '')
        elif tag == 'link' and attrs.get('rel') == 'canonical':
            self.canonical = attrs.get('href')
        elif tag == 'a' and attrs.get('href', '').startswith('https://t.me/share/url?'):
            self.share_href = attrs['href']
        elif tag == 'input' and attrs.get('id') == 'example-share-url':
            assert 'readonly' in attrs
            self.copy_url = attrs.get('value')


@pytest.mark.asyncio
async def test_public_social_previews_and_share_urls_never_include_visitor_query_data():
    app = create_admin_app(PrivateDBMustNotBeRead(), 'isolated-form-secret', 'test')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for path in ('/', '/example', '/how-it-works', '/guides/check-apartment-before-deposit'):
            page = await client.get(path + '?contact=PRIVATE-CONTACT&property_id=PRIVATE-ID')
            assert page.status_code == 200
            head = PublicPageHead(page.text)
            assert head.canonical == head.meta['og:url'] == 'https://hatuli.ai-groundtruth.com' + path
            assert head.meta['og:title'] == head.meta['twitter:title']
            assert head.meta['description'] == head.meta['og:description'] == head.meta['twitter:description']
            assert head.meta['og:image'] == head.meta['twitter:image'] == 'https://hatuli.ai-groundtruth.com/static/branding/hatuli-share.png'
            assert head.meta['twitter:card'] == 'summary_large_image'
            assert head.meta['og:image:width'] == '1200' and head.meta['og:image:height'] == '630'
            assert 'PRIVATE' not in json.dumps(head.meta)
            if path == '/example':
                assert head.copy_url == 'https://hatuli.ai-groundtruth.com/example'
                share = parse_qs(urlsplit(head.share_href).query)
                assert share['url'] == [head.copy_url]
                assert 'PRIVATE' not in head.share_href


@pytest.mark.asyncio
async def test_public_guide_is_linked_and_limits_are_explicit_without_private_database_access(monkeypatch):
    async def empty_public_complexes(query):
        assert 'FROM complexes' in query
        return []

    monkeypatch.setattr('bot.db.pg.fetch', empty_public_complexes)
    app = create_admin_app(PrivateDBMustNotBeRead(), 'isolated-form-secret', 'test')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        guide_path = '/guides/check-apartment-before-deposit'
        guide = await client.get(guide_path)
        assert guide.status_code == 200
        assert 'Заявки рассматриваем вручную' in guide.text
        assert 'не выполняет автоматическую проверку' in guide.text
        assert 'не выдаёт заключение о юридической чистоте' in guide.text
        for source in ('https://www.gov.kz/services/3857?lang=ru', 'https://www.gov.kz/services/3934?lang=ru', 'https://enis.kz/CheckWarrant', 'https://www.aerc.kz/ru/novosti/detail.php?ELEMENT_ID=6149687'):
            assert source in guide.text
        for path in ('/', '/example', '/llms.txt', '/sitemap.xml'):
            page = await client.get(path)
            assert page.status_code == 200
            assert guide_path in page.text
        sitemap = (await client.get('/sitemap.xml')).text
        assert '/listing/' not in sitemap and '/request-analysis' not in sitemap


@pytest.mark.parametrize('listing_meta', [None, {'title': 'Публичный объект', 'description': 'Описание объекта', 'image': None}, {'title': 'Публичный объект', 'description': 'Описание объекта', 'image': 'https://images.example/public.jpg'}])
def test_map_social_image_fallback_and_listing_image_have_one_consistent_preview(listing_meta):
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / 'bot' / 'templates'))
    templates.env.globals['is_admin'] = lambda request: False
    request = Request({'type': 'http', 'method': 'GET', 'path': '/map', 'scheme': 'https', 'server': ('hatuli.ai-groundtruth.com', 443), 'headers': [], 'query_string': b''})
    html = templates.get_template('dashboard.html').render(request=request, listing_meta=listing_meta, site_tier='public', stats={})
    head = PublicPageHead(html)
    expected_image = listing_meta['image'] if listing_meta and listing_meta['image'] else 'https://hatuli.ai-groundtruth.com/static/branding/hatuli-share.png'
    assert head.meta['og:image'] == head.meta['twitter:image'] == expected_image
    assert head.meta['og:title'] == head.meta['twitter:title']


def test_example_projection_omits_extra_private_fields_and_uses_actual_dates():
    raw = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    raw["property"].update({"id": "PRIVATE-ID", "seller_phone": "+77001234567", "address": "PRIVATE-ADDRESS"})
    raw["comparables"][0]["property_id"] = "PRIVATE-PROPERTY"
    public = build_public_example(raw)
    encoded = json.dumps(public, ensure_ascii=False)
    assert "PRIVATE" not in encoded and "+77001234567" not in encoded
    assert public["property"]["floor"] is None
    assert [p["price"] for p in public["price_history"]] == [34_000_000, 33_000_000, 34_000_000]
    points = public["chart"]["points"]
    assert points[1]["x"] - points[0]["x"] > points[2]["x"] - points[1]["x"]
    assert public["comparables"][0]["price_per_m2"] == pytest.approx(36_000_000 / 47)


@pytest.mark.parametrize("change", ["external_photo", "invalid_price", "future_observation", "wrong_complex", "wrong_area"])
def test_unreviewed_or_inconsistent_snapshot_fails_closed(change):
    raw = copy.deepcopy(json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")))
    if change == "external_photo":
        raw["property"]["photo_url"] = "https://seller.example/contact.jpg"
    elif change == "invalid_price":
        raw["property"]["price"] = float("nan")
    elif change == "future_observation":
        raw["price_history"][-1]["date"] = "2027-01-01"
    elif change == "wrong_complex":
        raw["comparables"][0]["complex_name"] = "Another physical complex"
    else:
        raw["comparables"][0]["area"] = 100
    with pytest.raises(ValueError):
        build_public_example(raw)
