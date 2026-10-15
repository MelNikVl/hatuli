"""Editorial routes render published data without DB access or visitor data."""
from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
import httpx
import pytest

from bot.web import analytics_articles as web


@pytest.fixture(autouse=True)
def no_postgres(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/hatuli_articles_test")

    async def reject(*args, **kwargs):
        raise AssertionError("Editorial page tests must not connect to PostgreSQL")

    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)


def prepared(kind="market", suffix=""):
    slug = "2026-10-16-" + kind + suffix
    return {
        "slug": slug, "url": "/analytics/" + slug,
        "title": "Как отличаются цены квартир в районах Астаны",
        "summary": "Сравниваем цены предложений и объясняем, на что смотреть при выборе.",
        "kind": kind, "category_label": "Аренда" if kind == "rent" else "Рынок жилья",
        "edition_date": "2026-10-16", "date_label": "16 октября 2026",
        "published_label": "16 октября 2026, 08:00 (UTC+5)", "reading_minutes": 4,
        "lead": "Цены отличаются, но сравнивать нужно квартиры близкой площади и комнатности.",
        "key_points": ["Медиана предложения — 650 тысяч ₸ за м².", "В выборке 120 объявлений.", "Разница районов не описывает отдельный дом."],
        "sections": [
            {"heading": "Цены по районам", "paragraphs": ["В Есильском районе медиана выше."], "chart_id": "district-price"},
            {"heading": "Как выбирать", "paragraphs": ["Сопоставьте бюджет с похожими квартирами."]},
            {"heading": "Что это значит", "paragraphs": ["Медиана не является оценкой цены сделки."]},
        ],
        "charts": [{
            "id": "district-price", "title": "Медиана цены за м²", "caption": "Сопоставимые активные объявления.",
            "unit": "тыс. ₸/м²", "axis_label": "Общая шкала: 0–800 тыс. ₸/м²",
            "rows": [
                {"label": "Есильский", "value_label": "720", "value": 720, "n": 70, "bar_pct": 90},
                {"label": "Алматинский", "value_label": "600", "value": 600, "n": 50, "bar_pct": 75},
            ],
        }],
        "sources": [{"label": "Наблюдения Hatuli", "url": "https://hatuli.ai-groundtruth.com/map", "as_of_label": "Наблюдения на 15 октября 2026, 23:00 (UTC+5)"}],
        "methodology": ["Одна запись на объявление. Цены предложений, не сделок."],
    }


def app(monkeypatch, articles):
    monkeypatch.setattr(web, "_load_articles", lambda category=None: deepcopy([item for item in articles if not category or item["kind"] == category]))
    monkeypatch.setattr(web, "_load_article", lambda slug: deepcopy(next((item for item in articles if item["slug"] == slug), None)))
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "bot" / "templates"))
    templates.env.globals["is_admin"] = lambda request: False
    application = FastAPI()
    application.include_router(web.make_analytics_articles_router(templates))
    return application


async def page(monkeypatch, articles, path="/analytics"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app(monkeypatch, articles)), base_url="http://test") as client:
        return await client.get(path)


class Parts(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.cards = 0
        self.charts = 0
        self.ids = []
        self.canonical = None
        self.meta = {}
        self.json_ld = []
        self.capture_json = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.cards += int("data-analytics-card" in attrs)
        self.charts += int("data-analytics-chart" in attrs)
        if attrs.get("id"):
            self.ids.append(attrs["id"])
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs["href"]
        if tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            if key:
                assert key not in self.meta
                self.meta[key] = attrs.get("content")
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.capture_json = True

    def handle_data(self, data):
        if self.capture_json:
            self.json_ld.append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            self.capture_json = False


@pytest.mark.asyncio
async def test_archive_has_real_articles_two_categories_unique_chart_ids_and_safe_metadata(monkeypatch):
    response = await page(monkeypatch, [prepared(), prepared("rent")], "/analytics?contact=PRIVATE-CONTACT")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["referrer-policy"] == "same-origin"
    parts = Parts(response.text)
    assert parts.cards == parts.charts == 2
    assert len(parts.ids) == len(set(parts.ids))
    assert 'href="/analytics?category=market"' in response.text
    assert 'href="/analytics?category=rent"' in response.text
    assert 'href="/analytics/2026-10-16-market"' in response.text
    assert 'href="/analytics/2026-10-16-rent"' in response.text
    assert "16 октября 2026" in response.text
    assert "PRIVATE" not in response.text
    assert parts.canonical == parts.meta["og:url"] == "https://hatuli.ai-groundtruth.com/analytics"
    assert parts.meta["description"] == parts.meta["og:description"] == parts.meta["twitter:description"]


@pytest.mark.asyncio
async def test_category_filter_and_pagination_preserve_only_supported_query_parameters(monkeypatch):
    articles = [prepared("rent", "-" + str(index)) for index in range(14)] + [prepared()]
    response = await page(monkeypatch, articles, "/analytics?category=rent&contact=PRIVATE")
    assert Parts(response.text).cards == 12
    assert 'href="/analytics?category=rent&amp;page=2"' in response.text
    assert 'href="/analytics/2026-10-16-market"' not in response.text
    assert "PRIVATE" not in response.text
    second = await page(monkeypatch, articles, "/analytics?category=rent&page=2")
    assert Parts(second.text).cards == 2
    assert 'href="/analytics?category=rent" rel="prev"' in second.text
    assert "Страница 2 из 2" in second.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path,status", [("/analytics?page=0", 422), ("/analytics?page=2", 404), ("/analytics?category=other", 422), ("/analytics/missing", 404)])
async def test_missing_articles_and_invalid_filters_do_not_render_fake_content(monkeypatch, path, status):
    response = await page(monkeypatch, [prepared()], path)
    assert response.status_code == status
    assert "data-analytics-card" not in response.text
    assert "data-analytics-article" not in response.text


@pytest.mark.asyncio
async def test_detail_has_takeaways_navigation_full_chart_table_sources_and_article_schema(monkeypatch):
    response = await page(monkeypatch, [prepared()], "/analytics/2026-10-16-market?contact=PRIVATE")
    assert response.status_code == 200
    parts = Parts(response.text)
    assert parts.charts == 1
    assert 'id="chart-district-price"' in response.text
    assert 'href="#section-3"' in response.text
    assert "Главное за минуту" in response.text
    assert "Данные графика" in response.text
    assert "Общая шкала: 0–800 тыс. ₸/м²" in response.text
    assert "70 объявлений" in response.text
    assert '<th scope="row">Есильский</th><td>720</td><td>70</td>' in response.text
    assert "Опубликовано" in response.text
    assert "16 октября 2026, 08:00" in response.text
    assert "Наблюдения на 15 октября 2026, 23:00" in response.text
    assert "Как считали и что важно учитывать" in response.text
    schema = json.loads("".join(parts.json_ld))
    assert schema["@type"] == "Article"
    assert schema["datePublished"] == "2026-10-16"
    assert schema["mainEntityOfPage"] == parts.canonical
    assert schema["headline"] == prepared()["title"]
    assert "PRIVATE" not in response.text


@pytest.mark.asyncio
async def test_editorial_text_and_json_ld_escape_script_markup(monkeypatch):
    article = prepared()
    attack = '</script><script>alert("UNSAFE")</script>'
    article.update(title=attack, summary=attack, lead=attack)
    article["key_points"][0] = attack
    article["sections"][0]["paragraphs"][0] = attack
    response = await page(monkeypatch, [article], "/analytics/2026-10-16-market")
    assert attack not in response.text
    assert "&lt;/script&gt;" in response.text
    assert json.loads("".join(Parts(response.text).json_ld))["headline"] == attack


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("bar_pct", "90%;background:red"), ("bar_pct", float("nan")), ("bar_pct", 101), ("bar_pct", -1), ("value", float("inf")), ("value", True), ("n", 0)])
async def test_invalid_chart_is_hidden_in_full_without_unsafe_or_misleading_widths(monkeypatch, field, value):
    article = prepared()
    article["charts"][0]["rows"][0][field] = value
    response = await page(monkeypatch, [article], "/analytics/2026-10-16-market")
    assert response.status_code == 200
    assert Parts(response.text).charts == 0
    assert "Цены по районам" in response.text
    assert "Данные графика" not in response.text
    assert "background:red" not in response.text


@pytest.mark.asyncio
async def test_empty_archive_has_useful_real_links_without_unavailable_data_notices(monkeypatch):
    response = await page(monkeypatch, [])
    assert response.status_code == 200
    assert Parts(response.text).cards == 0
    assert 'href="/best-apartments"' in response.text
    assert 'href="/best-rentals"' in response.text
    assert "недостаточно данных" not in response.text.lower()
    assert "Страница 1 из 1" not in response.text


@pytest.mark.asyncio
async def test_official_series_without_published_sample_counts_omit_count_column(monkeypatch):
    article = prepared()
    for row in article["charts"][0]["rows"]:
        row["n"] = None
    response = await page(monkeypatch, [article], "/analytics/2026-10-16-market")
    assert Parts(response.text).charts == 1
    assert "None" not in response.text
    assert "ha-chart-sample" not in response.text
    assert '<th scope="col">Объявлений</th>' not in response.text
    assert '<th scope="row">Есильский</th><td>720</td>' in response.text


@pytest.mark.asyncio
async def test_negative_series_uses_centered_zero_and_signed_direction(monkeypatch):
    article = prepared()
    article["charts"][0]["rows"][0].update(value=-7.2, value_label="−7,2%", bar_pct=90)
    response = await page(monkeypatch, [article], "/analytics/2026-10-16-market")
    assert Parts(response.text).charts == 1
    assert 'ha-chart-signed"' in response.text
    assert 'class="ha-chart-negative" style="width:45.0%"' in response.text
    assert "← Снижение · 0 · Рост →" in response.text
    assert "−7,2%" in response.text
