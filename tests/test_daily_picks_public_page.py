"""The public top-30 route reads a validated snapshot, never private databases."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import httpx
import pytest

from bot.admin_web import create_admin_app
from bot.core.daily_picks import LOCAL_TIMEZONE, SCHEMA_VERSION, validate_snapshot
from bot.core.daily_picks_display import prepare_daily_picks


class NoPrivateDB:
    def __getattr__(self, name):
        raise AssertionError(f"Public daily selection read private DB: {name}")


@pytest.fixture(autouse=True)
def no_postgres(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("Snapshot pages must not connect to PostgreSQL")
    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)


def snapshot(now):
    items = []
    for index in range(30):
        listing_id = str(1010000001 + index)
        items.append({
            "id": listing_id, "rank": index + 1,
            "source_url": f"https://krisha.kz/a/show/{listing_id}",
            "title": "2-комнатная квартира · 50 м²", "rooms": 2, "area": 50,
            "floor": 4, "floors_total": 12, "complex_name": "<script>UNSAFE</script>",
            "district": "Нура", "price": 34_000_000, "currency": "KZT", "price_per_m2": 680_000,
            "photo_urls": ["https://krisha-photos.kcdn.online/webp/10/01234567-89ab-cdef-0123-456789abcdef/1-full.webp"],
            "reasons": ["Цена за м² ниже медианы аналогов.", "Средний этаж: 4 из 12."],
            "cautions": [], "observed_at": now.isoformat(),
            "source_status": "active", "source_checked_at": now.isoformat(),
            "price_comparison": {"discount_pct": 2.9, "reference_price_per_m2": 700_000, "scope": "в том же ЖК", "comparable_count": 8},
            "seller_phone": "PRIVATE-PHONE", "private_note": "PRIVATE-NOTE",
        })
    from bot.core.daily_picks import METHODOLOGY
    return validate_snapshot({
        "schema_version": SCHEMA_VERSION, "timezone": "Asia/Almaty",
        "selection_date": now.astimezone(LOCAL_TIMEZONE).date().isoformat(),
        "generated_at": now.isoformat(), "source_observed_at": now.isoformat(),
        "total_candidates": 70, "items": items, "methodology": dict(METHODOLOGY),
        "raw_rows": "PRIVATE-ROWS",
    })


class Cards(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.cards = 0
        self.source_links = []
        self.feed(html)
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "article" and "data-picks-card" in attrs:
            self.cards += 1
        if tag == "a" and "picks-card-button" in attrs.get("class", ""):
            self.source_links.append(attrs["href"])


@pytest.mark.asyncio
async def test_anonymous_top30_is_public_safe_and_has_working_source_targets(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr("bot.admin_web.load_daily_picks", lambda: snapshot(now))
    app = create_admin_app(NoPrivateDB(), "test-form-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", cookies={"admin_auth": "1"}) as client:
        response = await client.get("/best-apartments?contact=PRIVATE-QUERY")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert "PRIVATE" not in response.text
    assert "<script>UNSAFE</script>" not in response.text
    assert "&lt;script&gt;UNSAFE&lt;/script&gt;" in response.text
    assert 'href="/admin/analysis-requests"' not in response.text
    cards = Cards(response.text)
    assert cards.cards == 30
    assert len(set(cards.source_links)) == 30
    assert all(link.startswith("https://krisha.kz/a/show/") for link in cards.source_links)
    assert "08:00" in response.text
    assert "https://hatuli.ai-groundtruth.com/best-apartments" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_missing_or_stale_snapshot_is_explicit_and_does_not_fail_page(monkeypatch, missing):
    older = datetime.now(timezone.utc) - timedelta(days=3)
    monkeypatch.setattr("bot.admin_web.load_daily_picks", lambda: None if missing else snapshot(older))
    app = create_admin_app(NoPrivateDB(), "test-form-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/best-apartments")
    assert response.status_code == 200
    assert "Готовим хорошие варианты" in response.text if missing else "Показываем последний выпуск" in response.text
    assert "на сегодня" not in response.text


def test_previous_day_before_schedule_is_labeled_as_previous_not_today():
    local_now = datetime(2026, 10, 14, 7, 30, tzinfo=LOCAL_TIMEZONE)
    previous = snapshot(datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc))
    display = prepare_daily_picks(previous, now=local_now)
    assert display["is_stale"] is False and display["is_today"] is False
    assert "предыдущая подборка" in display["status_message"]
    assert display["items"][0]["observed_at_label"] == "13 октября, 08:00"


@pytest.mark.asyncio
async def test_previous_day_before_schedule_never_claims_today_in_html_title(monkeypatch):
    previous = snapshot(datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc))
    local_now = datetime(2026, 10, 14, 7, 30, tzinfo=LOCAL_TIMEZONE)
    monkeypatch.setattr("bot.admin_web.load_daily_picks", lambda: previous)
    monkeypatch.setattr("bot.admin_web.prepare_daily_picks", lambda data: prepare_daily_picks(data, now=local_now))
    app = create_admin_app(NoPrivateDB(), "test-form-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/best-apartments")
    assert response.status_code == 200
    assert "на сегодня" not in response.text
    assert "13 октября 2026" in response.text
    assert "предыдущая подборка" in response.text
