"""The public top-30 route reads a validated snapshot, never private databases."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import httpx
import pytest

from bot.admin_web import create_admin_app
from bot.core.apartment_selection_reasons import best_apartment_reasons
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
    monkeypatch.setattr("bot.core.daily_picks_display.load_market_snapshot", lambda: {"offers": [], "listing_details": []})
    monkeypatch.setattr("bot.core.daily_picks_display.forecast_for_offer", lambda *args, **kwargs: {"status": "insufficient_history"})


def snapshot(now, count=30):
    items = []
    for index in range(count):
        listing_id = str(1010000001 + index)
        item = {
            "id": listing_id, "rank": index + 1,
            "source_url": f"https://krisha.kz/a/show/{listing_id}",
            "title": "2-комнатная квартира · 50 м²", "rooms": 2, "area": 50,
            "floor": 4, "floors_total": 12, "complex_name": "<script>UNSAFE</script>",
            "complex_identity": {"checked": True, "status": "catalog_bound", "canonical_id": 1,
                "canonical_name": "<script>UNSAFE</script>", "umbrella_id": None, "umbrella_name": None,
                "house_id": None, "house_label": None, "binding_method": "name"},
            "district": "Нура", "price": 34_000_000, "currency": "KZT", "price_per_m2": 680_000,
            "photo_urls": ["https://krisha-photos.kcdn.online/webp/10/01234567-89ab-cdef-0123-456789abcdef/1-full.webp"],
            "selection_facts": {"ceiling_height": 3.0, "kitchen_area": 12},
            "cautions": ["GENERIC-CHECKLIST-MUST-NOT-RENDER"], "observed_at": now.isoformat(),
            "source_status": "active", "source_checked_at": now.isoformat(),
            "price_comparison": {"discount_pct": 5.6, "reference_price_per_m2": 720_000, "scope": "в том же ЖК", "comparable_count": 8},
            "seller_phone": "PRIVATE-PHONE", "private_note": "PRIVATE-NOTE",
        }
        item["reasons"] = best_apartment_reasons(item, now)
        items.append(item)
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
        self.factor_counts = []
        self.current_card = None
        self.source_links = []
        self.feed(html)
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "article" and "data-picks-card" in attrs:
            self.cards += 1
            self.current_card = len(self.factor_counts)
            self.factor_counts.append(0)
        if tag == "li" and "data-picks-factor" in attrs and self.current_card is not None:
            self.factor_counts[self.current_card] += 1
        if tag == "a" and "picks-card-button" in attrs.get("class", ""):
            self.source_links.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "article":
            self.current_card = None


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
    assert cards.factor_counts == [4] * 30
    assert response.text.count("4 фактора выбора") == 30
    assert "минимум на 3% ниже похожих предложений" in response.text
    assert "Перед просмотром:" not in response.text
    assert "GENERIC-CHECKLIST-MUST-NOT-RENDER" not in response.text
    assert len(set(cards.source_links)) == 30
    assert all(link.startswith("https://krisha.kz/a/show/") for link in cards.source_links)
    assert "08:00" in response.text
    assert "https://hatuli.ai-groundtruth.com/best-apartments" in response.text


@pytest.mark.asyncio
async def test_ten_confirmed_cards_show_actual_count_and_four_supported_factors(monkeypatch):
    monkeypatch.setattr("bot.admin_web.load_daily_picks", lambda: snapshot(datetime.now(timezone.utc), count=10))
    app = create_admin_app(NoPrivateDB(), "test-form-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/best-apartments")
    assert response.status_code == 200
    cards = Cards(response.text)
    assert cards.cards == 10
    assert cards.factor_counts == [4] * 10
    assert "10 из 10 вариантов" in response.text
    assert "от 10 до 30 проверенных квартир" in response.text
    assert "3 фактора выбора" not in response.text


@pytest.mark.asyncio
async def test_public_page_excludes_over_budget_card_without_weakening_other_factors(monkeypatch):
    edition = snapshot(datetime.now(timezone.utc))
    excluded = edition["items"][0]
    excluded.update(price=120_000_000, price_per_m2=2_400_000)
    excluded["price_comparison"].update(discount_pct=5.5, reference_price_per_m2=2_540_000)
    monkeypatch.setattr("bot.admin_web.load_daily_picks", lambda: edition)
    app = create_admin_app(NoPrivateDB(), "test-form-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/best-apartments")
    assert response.status_code == 200
    cards = Cards(response.text)
    assert cards.cards == 29
    assert cards.factor_counts == [4] * 29
    assert excluded["source_url"] not in cards.source_links
    assert "120 000 000 ₸" not in response.text


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


@pytest.mark.asyncio
async def test_daily_cards_show_source_address_and_native_graph_for_any_price_change(monkeypatch):
    from bot.core.comparison_offer_details import build_offer_details
    now = datetime.now(timezone.utc)
    edition = snapshot(now)
    selected = edition["items"][0]
    selected["floor"] = None
    selected["selection_facts"].update(is_owner=True, owner_source_verified=True)
    detail = {"id": selected["id"], "area": selected["area"], "rooms": selected["rooms"],
              "current_price": selected["price"]}
    detail.update(build_offer_details({"price": selected["price"], "area": selected["area"],
        "ceiling_height": 3.0, "kitchen_area": 12, "address": "улица Сыганак, 12/1",
        "price_events": [{"old_price": 33_000_000, "new_price": 35_000_000, "changed_at": now-timedelta(days=3)},
                         {"old_price": 35_000_000, "new_price": 34_000_000, "changed_at": now-timedelta(days=1)}]},
        "secondary", now=now))
    monkeypatch.setattr("bot.admin_web.load_daily_picks", lambda: edition)
    monkeypatch.setattr("bot.core.daily_picks_display.load_market_snapshot", lambda: {"offers": [], "listing_details": [detail]})
    app = create_admin_app(NoPrivateDB(), "test-form-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/best-apartments")
    assert response.status_code == 200
    assert Cards(response.text).factor_counts == [4] * 30
    assert "Перед просмотром:" not in response.text
    assert "улица Сыганак, 12/1" in response.text
    assert "Точный адрес не указан в источнике" not in response.text
    assert response.text.count('class="picks-address"') == 1
    assert response.text.count('aria-label="История цены предложения"') == 1
    assert "Изменения цены пока не зафиксированы" not in response.text
    assert "История изменений цены пока не загружена" not in response.text
    assert "Этаж уточняйте у продавца" not in response.text
    assert response.text.count('class="picks-floor"') == 29
    assert "Цена и срок фактической продажи" not in response.text
    assert "недостаточно подтверждённых сделок" not in response.text
    assert 'data-price-history-chart' in response.text
    assert 'data-direction="up"' in response.text and 'data-direction="down"' in response.text
    assert "Все изменения по датам" in response.text
    assert "до 100 млн ₸" in response.text
    assert "33 000 000" in response.text and "35 000 000" in response.text and "34 000 000" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("has_ready_forecast", [False, True])
async def test_daily_page_only_renders_a_forecast_for_an_offer_with_validated_numbers(monkeypatch, has_ready_forecast):
    from tests.test_price_forecast_display import payload
    now = datetime.now(timezone.utc)
    edition = snapshot(now)
    selected_id = edition["items"][0]["id"]
    unavailable = {"as_of": now.isoformat(), "forecasts": [
        {"status": "insufficient_history", "horizon_days": 7,
         "reason": "NOT-FOR-BUYER: insufficient training history"}]}
    monkeypatch.setattr("bot.admin_web.load_daily_picks", lambda: edition)
    monkeypatch.setattr("bot.core.daily_picks_display.load_market_snapshot", lambda: {"offers": [], "listing_details": []})
    monkeypatch.setattr("bot.core.daily_picks_display.forecast_for_offer",
                        lambda identifier, *args, **kwargs: payload() if has_ready_forecast and identifier == selected_id else unavailable)
    app = create_admin_app(NoPrivateDB(), "test-form-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/best-apartments")
    assert response.status_code == 200
    cards = Cards(response.text)
    assert cards.cards == 30
    assert cards.factor_counts == [4] * 30
    assert "Перед просмотром:" not in response.text
    assert response.text.count('aria-label="Оценка цены и прогноз"') == int(has_ready_forecast)
    assert response.text.count("Что будет с ценой?") == int(has_ready_forecast)
    assert "NOT-FOR-BUYER" not in response.text
    assert "Пока недостаточно истории" not in response.text
    if has_ready_forecast:
        assert "31 500 000 ₸" in response.text and "Цена предложения через 7 дней" in response.text


@pytest.mark.asyncio
async def test_daily_card_retains_specific_history_gap_with_chart_without_generic_panel(monkeypatch):
    from bot.core.comparison_offer_details import build_offer_details
    now = datetime.now(timezone.utc)
    edition = snapshot(now)
    selected = edition["items"][0]
    detail = {"id": selected["id"], "area": selected["area"], "rooms": selected["rooms"],
              "current_price": selected["price"]}
    detail.update(build_offer_details({"price": selected["price"], "price_events": [
        {"old_price": 36_000_000, "new_price": 35_000_000, "changed_at": now-timedelta(days=2)},
        {"old_price": 34_500_000, "new_price": 34_000_000, "changed_at": now-timedelta(days=1)},
    ]}, "secondary", now=now))
    monkeypatch.setattr("bot.admin_web.load_daily_picks", lambda: edition)
    monkeypatch.setattr("bot.core.daily_picks_display.load_market_snapshot", lambda: {"offers": [], "listing_details": [detail]})
    app = create_admin_app(NoPrivateDB(), "test-form-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/best-apartments")
    assert response.status_code == 200
    assert Cards(response.text).factor_counts == [4] * 30
    assert 'class="picks-evidence-notices"' not in response.text
    assert "Между записанными изменениями цены есть пропуски" in response.text
    assert "Перед просмотром:" not in response.text
    assert "GENERIC-CHECKLIST-MUST-NOT-RENDER" not in response.text


@pytest.mark.asyncio
async def test_confirmed_house_in_umbrella_project_displays_both_labels_once(monkeypatch):
    edition = snapshot(datetime.now(timezone.utc))
    selected = edition["items"][0]
    selected["complex_identity"] = {"checked": True, "status": "house_from_source",
        "canonical_id": 2, "canonical_name": "Корпус 2", "umbrella_id": 1,
        "umbrella_name": "Тестовый ЖК", "house_id": 2, "house_label": "Корпус 2",
        "binding_method": "address"}
    selected["complex_name"] = "Корпус 2"
    monkeypatch.setattr("bot.admin_web.load_daily_picks", lambda: edition)
    app = create_admin_app(NoPrivateDB(), "test-form-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/best-apartments")
    assert response.status_code == 200
    assert 'data-picks-house-identity>Проект Тестовый ЖК · Дом Корпус 2 · Нура</p>' in response.text
    assert response.text.count("Корпус 2") == 1
    assert response.text.count("data-picks-house-identity") == 1
    assert Cards(response.text).factor_counts == [4] * 30
