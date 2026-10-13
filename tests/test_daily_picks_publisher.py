"""Publisher boundaries exercised with fake PostgreSQL and HTTP transports."""
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from scripts import publish_daily_picks as publisher


NOW = datetime(2026, 10, 13, 4, tzinfo=timezone.utc)
PHOTO_ROOT = "https://krisha-photos.kcdn.online/webp/c7/c70532b0-9310-4f48-bc2d-e59a83f9f345/"


@pytest.fixture(autouse=True)
def prohibit_real_database_connections(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("Publisher tests cannot connect to PostgreSQL")

    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)


class FakeTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        self.connection.in_transaction = True

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.in_transaction = False
        self.connection.transaction_error = exc_type


class FakeConnection:
    def __init__(self, *, failure=None):
        self.failure = failure
        self.in_transaction = False
        self.closed = False
        self.transaction_options = None
        self.transaction_error = None

    def transaction(self, **options):
        self.transaction_options = options
        return FakeTransaction(self)

    async def fetch(self, query, observed_at):
        assert self.in_transaction, "Candidate reads must occur inside the transaction"
        assert self.transaction_options["readonly"] is True
        assert observed_at == NOW
        assert query == publisher.CANDIDATES_SQL
        if self.failure:
            raise self.failure
        return [{"id": "1016000001", "price": 30_000_000}]

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_reader_enforces_read_only_session_and_transaction_and_closes(monkeypatch):
    connection = FakeConnection()
    calls = []

    async def connect(dsn, **options):
        calls.append((dsn, options))
        return connection

    monkeypatch.setattr("asyncpg.connect", connect)
    result = await publisher.read_candidates("postgresql://unit_test/unused", NOW)
    assert result == [{"id": "1016000001", "price": 30_000_000}]
    assert calls[0][1]["server_settings"]["default_transaction_read_only"] == "on"
    assert calls[0][1]["command_timeout"] == 60
    assert connection.transaction_options == {"isolation": "repeatable_read", "readonly": True}
    assert connection.closed and not connection.in_transaction


def test_publisher_selects_original_address_but_keeps_comparable_population():
    assert "a.address" in publisher.CANDIDATES_SQL
    assert "100000000" not in publisher.CANDIDATES_SQL.replace("_", "")
    assert all(field not in publisher.CANDIDATES_SQL for field in ("a.phone", "a.description", "a.seller_name"))


@pytest.mark.asyncio
async def test_query_failure_exits_transaction_and_closes_connection(monkeypatch):
    connection = FakeConnection(failure=RuntimeError("simulated database failure"))

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr("asyncpg.connect", connect)
    with pytest.raises(RuntimeError, match="simulated database failure"):
        await publisher.read_candidates("postgresql://unit_test/unused", NOW)
    assert connection.transaction_error is RuntimeError
    assert connection.closed and not connection.in_transaction


@pytest.mark.asyncio
async def test_photo_checks_drop_failures_reject_redirects_and_renumber(monkeypatch):
    calls = []

    def request_handler(request):
        calls.append((request.method, str(request.url)))
        if request.url.path.endswith("2-full.jpg"):
            return httpx.Response(200, headers={"content-type": "image/jpeg"})
        if request.url.path.endswith("3-full.jpg"):
            return httpx.Response(302, headers={"location": "https://private.example/image.jpg"})
        return httpx.Response(200, headers={"content-type": "text/html"})

    original_client = httpx.AsyncClient

    def fake_client(**options):
        assert options["follow_redirects"] is False
        return original_client(transport=httpx.MockTransport(request_handler), **options)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    snapshot = {"items": [
        {"id": "1016000001", "rank": 1, "photo_urls": [PHOTO_ROOT + "1-full.jpg", PHOTO_ROOT + "3-full.jpg"]},
        {"id": "1016000002", "rank": 2, "photo_urls": ["https://unsafe.example/photo.jpg", PHOTO_ROOT + "2-full.jpg"]},
    ]}
    checked, count = await publisher.verify_photos(snapshot)
    assert count == 3
    assert calls == [("HEAD", PHOTO_ROOT + name) for name in ("1-full.jpg", "3-full.jpg", "2-full.jpg")]
    assert checked["items"] == [{"id": "1016000002", "rank": 1, "photo_urls": [PHOTO_ROOT + "2-full.jpg"]}]
    assert snapshot["items"][1]["rank"] == 2


@pytest.mark.asyncio
async def test_expired_photo_budget_fails_before_any_http_request(monkeypatch):
    def reject(request):
        raise AssertionError("No HTTP request is allowed after the budget expires")

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: original_client(transport=httpx.MockTransport(reject), **options))
    monkeypatch.setattr(publisher.time, "monotonic", lambda: 100)
    with pytest.raises(TimeoutError, match="budget exhausted"):
        await publisher.verify_photos({"items": [{"photo_urls": [PHOTO_ROOT + "1-full.jpg"]}]}, deadline=99)


@pytest.mark.asyncio
async def test_refill_replaces_failed_cards_without_changing_comparable_pool(monkeypatch):
    rows = [dict(id=str(1016000000 + index), property_id=index,
        url=f"https://krisha.kz/a/show/{1016000000 + index}",
        is_active=True, market_type="secondary", price=30_000_000,
        area=60, rooms=2, last_seen=NOW, complex_id=1, complex_resolution="name",
        floor=5, floors_total=12, approved_photo_urls=[PHOTO_ROOT + "1-full.jpg"],
        finish_level="finished") for index in range(60)]
    batches = []

    async def read(dsn, now):
        assert dsn == "postgresql://unit_test/unused" and now == NOW
        return rows

    async def verify(snapshot, deadline):
        assert deadline > publisher.time.monotonic()
        batches.append(snapshot)
        kept = snapshot["items"][:20] if len(batches) == 1 else snapshot["items"]
        return {**snapshot, "items": kept}, len(snapshot["items"])

    async def verify_sources(snapshot, deadline):
        return {**snapshot, "items": [{**item, "source_status": "active", "source_checked_at": NOW.isoformat()} for item in snapshot["items"]]}, len(snapshot["items"]), set()

    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unit_test/unused")
    monkeypatch.setattr(publisher, "read_candidates", read)
    monkeypatch.setattr(publisher, "verify_sources", verify_sources)
    monkeypatch.setattr(publisher, "verify_photos", verify)
    result = await publisher.generate(SimpleNamespace(skip_photo_check=False), NOW)
    items = result["snapshot"]["items"]
    assert len(batches) == 2 and len(items) == 30
    assert len({item["id"] for item in items}) == 30
    assert [item["rank"] for item in items] == list(range(1, 31))
    assert all(item["price_comparison"]["comparable_count"] == 59 for item in items)
    assert result["photo_heads"] == 60


def test_daily_job_retains_previous_file_when_fewer_than_30_qualify(tmp_path, monkeypatch, capsys):
    path = tmp_path / "daily-picks.json"
    path.write_bytes(b"previous-public-snapshot")

    @contextmanager
    def lock(target):
        assert target == path
        yield

    async def generate(args, now):
        assert args.allow_partial is False
        return {"snapshot": {"items": [{} for _ in range(29)]}}

    def reject_publish(*args, **kwargs):
        raise AssertionError("An incomplete daily edition must not replace the previous file")

    monkeypatch.setattr(publisher.sys, "argv", ["publish_daily_picks.py"])
    monkeypatch.setattr(publisher, "snapshot_path", lambda: path)
    monkeypatch.setattr(publisher, "publisher_lock", lock)
    monkeypatch.setattr(publisher, "load_daily_picks", lambda target: None)
    monkeypatch.setattr(publisher, "generate", generate)
    monkeypatch.setattr(publisher, "publish_snapshot", reject_publish)
    assert publisher.main() == 1
    assert path.read_bytes() == b"previous-public-snapshot"
    assert '"previous_snapshot_retained": true' in capsys.readouterr().err


def source_item(index=1, **changes):
    listing_id = str(1016000000 + index)
    return {"id": listing_id, "source_url": f"https://krisha.kz/a/show/{listing_id}",
        "price": 30_000_000, "rooms": 2, "area": 60.4, "floor": 5, "floors_total": 12, **changes}


def source_html(item, *, badge="", price=None, canonical=None, title=None, outside=""):
    canonical = canonical or item["source_url"]
    price = price if price is not None else "30 000 000 ₸"
    title = title or "2-комнатная квартира, 60,4 м², 5/12 этаж"
    return f"""<!doctype html><html><head><link rel="canonical" href="{canonical}"></head>
      <body><nav>Мои объявления: В архиве</nav><main class="offer">
      <nav class="bread-crumbs"><a href="/prodazha/kvartiry/astana/">Квартиры в Астане</a></nav>
      <h1>{title}</h1><div class="offer__price">{price}</div>{badge}</main>{outside}</body></html>"""


def test_main_offer_identity_current_price_and_archive_status_are_required():
    item = source_item()
    # A navigation link outside the offer must not make an active offer archived.
    assert publisher.parse_source_offer(source_html(item), item) == "active"
    assert publisher.parse_source_offer(source_html(item, badge='<div class="offer__status">В архиве</div>'), item) == "archived"
    assert publisher.parse_source_offer(source_html(item, badge='<div>Объявление может быть неактуальным</div>'), item) == "archived"
    assert publisher.parse_source_offer(source_html(item, price="31 000 000 ₸"), item) == "mismatch"
    assert publisher.parse_source_offer(source_html(item, title="3-комнатная квартира, 60,4 м², 5/12 этаж"), item) == "mismatch"
    assert publisher.parse_source_offer(source_html(item, title="2-комнатная квартира, 75 м², 5/12 этаж"), item) == "mismatch"
    assert publisher.parse_source_offer(source_html(item, title="2-комнатная квартира, 60,4 м², 12/12 этаж"), item) == "mismatch"


@pytest.mark.parametrize("html", [
    "<html><h1>Checking your browser</h1></html>",
    '<html><main><a href="https://krisha.kz/a/show/1016000001">Популярная квартира</a></main></html>',
    source_html(source_item(), canonical="https://krisha.kz/a/show/1016000999"),
    source_html(source_item(), price="от 30 000 000 ₸"),
    source_html(source_item(), price="30 000 000 USD"),
    source_html(source_item()).replace("/prodazha/kvartiry/astana/", "/arenda/kvartiry/astana/"),
    source_html(source_item()).replace("/prodazha/kvartiry/astana/", "/prodazha/kvartiry/almaty/"),
])
def test_generic_pages_wrong_identity_or_unscoped_prices_cannot_confirm_active(html):
    assert publisher.parse_source_offer(html, source_item()) == "unknown"


def test_recommendation_content_outside_empty_main_cannot_confirm_main_offer():
    item = source_item()
    html = source_html(item).replace('<main class="offer">', '<section class="recommendations">').replace('</main>', '</section><main></main>')
    assert publisher.parse_source_offer(html, item) == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 468])
async def test_source_block_stops_before_next_request_without_alternate_transport(monkeypatch, status):
    calls = []

    def handler(request):
        calls.append((request.method, str(request.url)))
        return httpx.Response(status)

    original_client = httpx.AsyncClient

    def client(**options):
        assert options["follow_redirects"] is False
        return original_client(transport=httpx.MockTransport(handler), **options)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    with pytest.raises(publisher.SourceBlockedError) as raised:
        await publisher.verify_sources({"items": [source_item(1), source_item(2)]})
    assert raised.value.status == status
    assert calls == [("GET", source_item(1)["source_url"])]


@pytest.mark.asyncio
async def test_source_verification_rejects_archived_deleted_changed_and_generic_offers(monkeypatch):
    statuses = {}
    for index in range(1, 7):
        item = source_item(index)
        if index == 1:
            statuses[item["source_url"]] = httpx.Response(200, text=source_html(item), headers={"content-type": "text/html"})
        elif index == 2:
            statuses[item["source_url"]] = httpx.Response(200, text=source_html(item, badge="<div>В архиве</div>"), headers={"content-type": "text/html"})
        elif index == 3:
            statuses[item["source_url"]] = httpx.Response(404)
        elif index == 4:
            statuses[item["source_url"]] = httpx.Response(200, text=source_html(item, price="31 000 000 ₸"), headers={"content-type": "text/html"})
        elif index == 5:
            statuses[item["source_url"]] = httpx.Response(200, text="<main>Рекомендуемые квартиры</main>", headers={"content-type": "text/html"})
        else:
            statuses[item["source_url"]] = httpx.Response(200, text=source_html(item), headers={"content-type": "application/json"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: original_client(transport=httpx.MockTransport(lambda request: statuses[str(request.url)]), **options))

    async def no_delay(*args):
        return None

    monkeypatch.setattr(publisher.asyncio, "sleep", no_delay)
    verified, count, invalid = await publisher.verify_sources({"items": [source_item(i) for i in range(1, 7)]})
    assert count == 6
    assert [item["id"] for item in verified["items"]] == [source_item(1)["id"]]
    assert verified["items"][0]["source_status"] == "active"
    assert datetime.fromisoformat(verified["items"][0]["source_checked_at"]).tzinfo is not None
    assert invalid == {source_item(i)["id"] for i in (2, 3, 4)}


@pytest.mark.asyncio
async def test_confirmed_archived_sources_are_removed_from_final_comparison_pool(monkeypatch):
    rows = [dict(id=str(1016000000 + index), property_id=index,
        url=f"https://krisha.kz/a/show/{1016000000 + index}",
        is_active=True, market_type="secondary", price=30_000_000,
        area=60, rooms=2, last_seen=NOW, complex_id=1, complex_resolution="name",
        floor=5, floors_total=12, approved_photo_urls=[PHOTO_ROOT + "1-full.jpg"],
        finish_level="finished") for index in range(60)]
    source_batches = []
    archived = set()

    async def read(dsn, now):
        return rows

    async def verify_sources(snapshot, deadline):
        source_batches.append(snapshot)
        rejected = {item["id"] for item in snapshot["items"][:5]} if len(source_batches) == 1 else set()
        archived.update(rejected)
        confirmed = [{**item, "source_status": "active", "source_checked_at": NOW.isoformat()} for item in snapshot["items"] if item["id"] not in rejected]
        return {**snapshot, "items": confirmed}, len(snapshot["items"]), rejected

    async def verify_photos(snapshot, deadline):
        return snapshot, len(snapshot["items"])

    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unit_test/unused")
    monkeypatch.setattr(publisher, "read_candidates", read)
    monkeypatch.setattr(publisher, "verify_sources", verify_sources)
    monkeypatch.setattr(publisher, "verify_photos", verify_photos)
    result = await publisher.generate(SimpleNamespace(skip_photo_check=False), NOW)
    items = result["snapshot"]["items"]
    assert len(items) == 30 and len(source_batches) == 2
    assert not archived.intersection(item["id"] for item in items)
    assert all(item["price_comparison"]["comparable_count"] == 54 for item in items)
    assert all(item["source_status"] == "active" for item in items)


def test_blocked_daily_job_retains_previous_file_without_claiming_success(tmp_path, monkeypatch, capsys):
    path = tmp_path / "daily-picks.json"
    path.write_bytes(b"previous-source-verified-public-snapshot")

    @contextmanager
    def lock(target):
        yield

    async def generate(args, now):
        raise publisher.SourceBlockedError(468)

    def reject_publish(*args, **kwargs):
        raise AssertionError("Blocked source checks must never publish")

    monkeypatch.setattr(publisher.sys, "argv", ["publish_daily_picks.py"])
    monkeypatch.setattr(publisher, "snapshot_path", lambda: path)
    monkeypatch.setattr(publisher, "publisher_lock", lock)
    monkeypatch.setattr(publisher, "load_daily_picks", lambda target: None)
    monkeypatch.setattr(publisher, "generate", generate)
    monkeypatch.setattr(publisher, "publish_snapshot", reject_publish)
    assert publisher.main() == 1
    assert path.read_bytes() == b"previous-source-verified-public-snapshot"
    output = capsys.readouterr()
    assert not output.out
    assert '"error_type": "SourceBlockedError"' in output.err
    assert '"previous_snapshot_retained": true' in output.err
