"""Public monthly picks render a prepared artifact without private DB access."""
from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser

import httpx
import pytest

from bot.admin_web import create_admin_app
from bot.web import rental_picks as web


class NoPrivateDB:
    def __getattr__(self, name):
        raise AssertionError(f"Rental selection must not read a private DB: {name}")


@pytest.fixture(autouse=True)
def no_postgres(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/hatuli_rental_picks_test")
    async def reject(*args, **kwargs):
        raise AssertionError("Public snapshot tests must not connect to PostgreSQL")
    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)


def prepared():
    now = datetime.now(timezone.utc).isoformat()
    return {
        "selection_date": now[:10], "date_label": "16 октября 2026",
        "generated_at_label": "16 октября, 08:05", "as_of_label": "16 октября, 08:00",
        "is_today": True, "is_stale": False, "status_message": "",
        "methodology": {"summary": "Цена за м² ниже сопоставимых помесячных предложений."},
        "items": [{
            "id": "1007342777", "rank": 1, "source_url": "https://krisha.kz/a/show/1007342777",
            "title": "2-комнатная квартира · 65 м²", "rooms": 2, "area": 65,
            "floor": 8, "floors_total": 11, "district": "Есильский",
            "address": "Туран, 12", "complex_name": "<script>UNSAFE</script>",
            "price": 250000, "price_per_m2": 3846,
            "photo_urls": ["https://krisha-photos.kcdn.online/webp/10/01234567-89ab-cdef-0123-456789abcdef/1-full.webp"],
            "reasons": ["На 10% ниже похожих квартир", "Площадь 65 м²", "8 этаж из 11", "От хозяина"],
            "source_checked_at_label": "16 октября, 08:00", "observed_at_label": "16 октября, 07:50",
            "price_history": {"status": "complete", "events": [{
                "at": "2026-10-15T12:00:00+00:00", "date_label": "15.10.2026",
                "old_price": 280000, "new_price": 250000}]},
            "rental_forecast": {},
        }],
    }


async def page(monkeypatch, snapshot):
    monkeypatch.setattr(web, "_published_selection", lambda: deepcopy(snapshot))
    app = create_admin_app(NoPrivateDB(), "test-form-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/best-rentals?contact=PRIVATE-QUERY")


class PageParts(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.cards = 0
        self.factors = 0
        self.current_page = []
        self.sources = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "article" and "data-picks-card" in attrs:
            self.cards += 1
        if tag == "li" and "data-picks-factor" in attrs:
            self.factors += 1
        if tag == "a" and attrs.get("aria-current") == "page":
            self.current_page.append(attrs.get("href"))
        if tag == "a" and "picks-card-button" in attrs.get("class", ""):
            self.sources.append(attrs["href"])


@pytest.mark.asyncio
async def test_anonymous_rental_cards_have_four_reasons_addresses_and_monthly_units(monkeypatch):
    response = await page(monkeypatch, prepared())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["referrer-policy"] == "same-origin"
    parsed = PageParts(response.text)
    assert parsed.cards == 1 and parsed.factors == 4
    assert parsed.current_page == ["/best-rentals"]
    assert parsed.sources == ["https://krisha.kz/a/show/1007342777"]
    assert "250 000 ₸/мес" in response.text
    assert "3 846 ₸ за м² в месяц" in response.text
    assert "Туран, 12" in response.text
    assert "8 этаж из 11" in response.text
    assert "между первым и последним" not in response.text
    assert "<script>UNSAFE</script>" not in response.text
    assert "&lt;script&gt;UNSAFE&lt;/script&gt;" in response.text
    assert "PRIVATE" not in response.text
    assert 'data-price-history-chart' in response.text
    assert 'data-rental-forecast-status' not in response.text
    assert "Пока недостаточно" not in response.text
    assert "Квартира или вклад" not in response.text
    assert "https://hatuli.ai-groundtruth.com/best-rentals" in response.text


@pytest.mark.asyncio
async def test_no_snapshot_has_real_tool_links_and_no_fabricated_cards(monkeypatch):
    response = await page(monkeypatch, None)
    assert response.status_code == 200
    assert PageParts(response.text).cards == 0
    assert 'href="/rent#rent-calculator"' in response.text
    assert 'href="/rental/daily"' in response.text
    assert 'href="#apartments"' not in response.text
    assert "Готовим" not in response.text
    assert "нет данных" not in response.text.lower()
    assert 'data-rental-forecast-status' not in response.text


@pytest.mark.asyncio
async def test_stale_edition_is_explicit_and_not_labeled_today(monkeypatch):
    snapshot = prepared()
    snapshot.update(is_stale=True, is_today=False, status_message="Проверка объявления выполнена 15 октября.")
    response = await page(monkeypatch, snapshot)
    assert "Последний выпуск — 16 октября 2026" in response.text
    assert "Проверка объявления выполнена 15 октября" in response.text
    assert "на сегодня" not in response.text


def forecast(kind):
    if kind == "asking_price":
        return {"estimate": 245000, "low": 230000, "high": 260000, "horizon_days": 14,
                "as_of_label": "16.10.2026", "target_date_label": "30.10.2026"}
    return {"median_days": 18, "low_days": 9, "high_days": 32, "as_of_label": "16.10.2026"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["asking_price", "removal"])
async def test_ready_forecast_renders_only_its_own_target(monkeypatch, kind):
    snapshot = prepared()
    snapshot["items"][0]["rental_forecast"] = {kind: forecast(kind)}
    response = await page(monkeypatch, snapshot)
    assert 'data-rental-forecast-status="ready"' in response.text
    if kind == "asking_price":
        assert "Прогноз цены объявления через 14 дней" in response.text
        assert "245 000 ₸/мес" in response.text
        assert "Ожидаемое снятие" not in response.text
    else:
        assert "Ожидаемое снятие объявления через" in response.text
        assert "18 дн." in response.text
        assert "Снятие объявления с публикации не означает, что квартира сдана" in response.text
        assert "Прогноз цены объявления через" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,change", [
    ("asking_price", {"estimate": None}), ("asking_price", {"estimate": True}),
    ("asking_price", {"estimate": float("nan")}), ("asking_price", {"estimate": float("inf")}),
    ("asking_price", {"low": 280000}), ("asking_price", {"target_date_label": ""}),
    ("removal", {"median_days": None}), ("removal", {"median_days": True}),
    ("removal", {"low_days": -1}), ("removal", {"high_days": 10}),
    ("removal", {"as_of_label": ""}),
])
async def test_incomplete_or_invalid_forecast_hides_the_whole_block(monkeypatch, kind, change):
    snapshot = prepared()
    snapshot["items"][0]["rental_forecast"] = {kind: {**forecast(kind), **change}}
    response = await page(monkeypatch, snapshot)
    assert 'data-rental-forecast-status' not in response.text
    assert "Цена и срок публикации" not in response.text


@pytest.mark.asyncio
async def test_sale_switch_and_rental_tools_link_are_reachable(monkeypatch):
    monkeypatch.setattr("bot.admin_web.load_daily_picks", lambda: None)
    monkeypatch.setattr("bot.web.rental_tools.load_market_snapshot", lambda: {"offers": [], "listing_details": []})
    app = create_admin_app(NoPrivateDB(), "test-form-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        sale = await client.get("/best-apartments")
        rental_tools = await client.get("/rent")
    assert sale.status_code == 200
    assert set(PageParts(sale.text).current_page) == {"/best-apartments"}
    assert 'href="/best-rentals"' in sale.text
    assert rental_tools.status_code == 200
    assert 'href="/best-rentals"' in rental_tools.text
