"""Automatic metadata preserves published facts and never needs PostgreSQL."""
from copy import deepcopy
from html.parser import HTMLParser
import json

import pytest

from bot.core.article_seo import article_seo
from tests.test_analytics_articles_public_page import Parts, page, prepared


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/hatuli_seo_test")

    async def deny(*args, **kwargs):
        raise AssertionError("Article metadata must not connect to PostgreSQL")

    monkeypatch.setattr("asyncpg.connect", deny)
    monkeypatch.setattr("asyncpg.create_pool", deny)
    monkeypatch.setattr("bot.db.pg.init_pool", deny)


class Titles(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.inside_title = self.inside_h1 = False
        self.title, self.h1 = [], []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.inside_title = True
        if tag == "h1":
            self.inside_h1 = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.inside_title = False
        if tag == "h1":
            self.inside_h1 = False

    def handle_data(self, value):
        if self.inside_title:
            self.title.append(value)
        if self.inside_h1:
            self.h1.append(value)


def test_valid_short_summary_remains_complete_with_truthful_publication_date():
    article = prepared()
    result = article_seo(article)
    assert result["title"] == article["title"] + " · 16.10.2026 — Hatuli"
    assert result["description"] == article["summary"] + " Выпуск от 16.10.2026."
    assert "Астана" not in result["description"]  # Never insert a location just for keywords.


def test_no_hard_truncation_drops_a_number_negation_period_or_qualification():
    article = prepared()
    article.update(title="Сколько стоит квартира 61,5 м²: предложение не равно сделке",
                   summary=" ".join(["Сравнение не подтверждает цену сделки."] * 8)
                   + " В сентябре 2026 года цена — 31 750 000 ₸; это не прогноз доходности.")
    result = article_seo(article)
    assert result["description"].startswith(article["summary"])
    for token in ("31 750 000 ₸", "В сентябре 2026 года", "это не прогноз доходности"):
        assert token in result["description"]
    assert "61,5 м²" in result["title"]
    assert "предложение не равно сделке" in result["title"]


def test_normalization_decodes_entities_once_and_preserves_multilingual_names():
    result = article_seo({"title": '  ЖК &quot;Álem / Бағыстан&quot;\n—\tцена 55,5 м² ',
                          "summary": "  Қазан 2026: 230&nbsp;000 ₸/мес.\nНе цена договора. ",
                          "edition_date": "2026-10-16"})
    assert result["title"] == 'ЖК "Álem / Бағыстан" — цена 55,5 м² · 16.10.2026 — Hatuli'
    assert result["description"] == "Қазан 2026: 230 000 ₸/мес. Не цена договора. Выпуск от 16.10.2026."


def test_date_is_original_edition_not_revision_date_or_clock_and_helper_does_not_mutate():
    article = prepared()
    before = deepcopy(article)
    initial = article_seo(article)
    article["date_modified"] = "2035-12-01T12:00:00+05:00"
    assert article_seo(article) == initial
    article.pop("date_modified")
    assert article == before
    second = {**article, "edition_date": "2026-10-17"}
    assert article_seo(second)["title"] != initial["title"]
    assert article_seo(second)["description"] != initial["description"]


@pytest.mark.parametrize("date", [None, "", "not-a-date", "2026-13-01", "2026-02-30", "2026-1-01"])
def test_invalid_dates_are_omitted_without_inventing_today(date):
    result = article_seo({"title": "Разбор условий договора", "summary": "Описание этого материала.", "edition_date": date})
    assert result["title"] == "Разбор условий договора — Hatuli"
    assert result["description"] == "Описание этого материала."


def test_missing_text_falls_back_only_to_existing_content():
    result = article_seo({"title": "Условия аренды", "summary": "  ", "lead": "Срок договора не равен сроку поиска жильца."})
    assert result["description"] == "Срок договора не равен сроку поиска жильца."
    assert article_seo({"title": "Условия договора"})["description"] == "Условия договора"
    empty = article_seo({})
    assert empty == {"title": "Аналитика — Hatuli", "description": ""}
    assert not any(word in repr(empty) for word in ("выгод", "цен", "район", "доход"))


def test_abnormally_large_values_fail_to_real_fallback_instead_of_cutting_a_fact():
    result = article_seo({"title": "ЖК " + "Дом " * 300, "category_label": "Аренда",
                          "summary": "Цена не " + "очень " * 400, "lead": "Фактическое описание материала."})
    assert result["title"] == "Аренда — Hatuli"
    assert result["description"] == "Фактическое описание материала."


@pytest.mark.asyncio
async def test_server_meta_og_twitter_and_schema_agree_without_query_or_private_data(monkeypatch):
    article = prepared()
    article["title"] = 'ЖК «Бағыстан» и "Álem": аренда не равна доходности'
    article["summary"] = 'При 230 000 ₸ в месяц расходы не равны нулю. Сравниваем "чистый" результат.'
    response = await page(monkeypatch, [article], article["url"] + "?contact=PRIVATE-CONTACT")
    assert response.status_code == 200
    parts, text = Parts(response.text), Titles(response.text)
    seo = article_seo(article)
    assert "".join(text.title) == parts.meta["og:title"] == parts.meta["twitter:title"] == seo["title"]
    assert parts.meta["description"] == parts.meta["og:description"] == parts.meta["twitter:description"] == seo["description"]
    schema = json.loads("".join(parts.json_ld))
    assert schema["description"] == seo["description"]
    assert schema["headline"] == "".join(text.h1) == article["title"]
    assert schema["datePublished"] == article["edition_date"]
    assert parts.meta["og:type"] == "article"
    assert parts.canonical == parts.meta["og:url"] == schema["mainEntityOfPage"]
    assert parts.canonical == "https://hatuli.ai-groundtruth.com" + article["url"]
    assert "PRIVATE" not in response.text


@pytest.mark.asyncio
async def test_quotes_entities_and_script_markup_are_escaped_in_every_context(monkeypatch):
    article = prepared()
    attack = '\"><script>alert("UNSAFE")</script>'
    article.update(title=attack, summary='ЖК &quot;Álem&quot;: ' + attack)
    response = await page(monkeypatch, [article], article["url"])
    assert attack not in response.text
    assert "<script>alert" not in response.text
    parts, text = Parts(response.text), Titles(response.text)
    assert "".join(text.title) == parts.meta["og:title"] == parts.meta["twitter:title"]
    assert parts.meta["description"] == parts.meta["og:description"] == parts.meta["twitter:description"]
    schema = json.loads("".join(parts.json_ld))
    assert schema["headline"] == attack
    assert schema["description"] == article_seo(article)["description"]
    assert 'ЖК "Álem"' in parts.meta["description"]


@pytest.mark.asyncio
async def test_helper_never_scans_archive_for_uniqueness_and_non_article_og_type_is_unchanged(monkeypatch):
    from bot.web import analytics_articles as web
    from tests.test_analytics_articles_public_page import app
    import httpx
    article = prepared()
    application = app(monkeypatch, [article])
    monkeypatch.setattr(web, "_load_articles", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("No archive scan for detail SEO")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.get(article["url"])
    assert response.status_code == 200
    assert Parts(response.text).meta["og:type"] == "article"
    archive = await page(monkeypatch, [article])
    assert Parts(archive.text).meta["og:type"] == "website"
