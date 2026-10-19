"""Native immutable archives reach public routes without mocked article loaders."""
from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
import httpx
import pytest

from bot.core import analytics_articles as core
from bot.web import analytics_articles as web
from tests.test_analytics_articles import NOW, digest, edition, observed_edition


DAY = "2026-10-16"
PLACE = 'ЖК Geneva 4 "Парк"'


@pytest.fixture(autouse=True)
def deny_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/hatuli_local_articles_test")

    async def reject(*args, **kwargs):
        raise AssertionError("Local article route tests must not initialize or connect to PostgreSQL")

    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    monkeypatch.setattr("bot.db.pg.init_pool", reject)


def local_edition():
    value = observed_edition()
    value["digest"]["sources"]["bns.housing"]["label"] = "Синтетические наблюдения Hatuli для изолированного теста"
    value["digest"]["facts"]["sales.rooms_1"]["label"] = PLACE
    value["articles"][0]["lead"] += " Для примера рассматриваем {label:sales.rooms_1} в пределах сопоставимого профиля."
    return value


def publish_base(root):
    assert core.publish_edition(edition(), root=root, now=NOW)["status"] == "published"
    return root / "var/analytics-articles" / f"{DAY}.json"


def frozen(*paths):
    return [(path, path.read_bytes(), path.stat().st_mtime_ns) for path in paths]


def assert_frozen(snapshots):
    for path, content, mtime in snapshots:
        assert (path.read_bytes(), path.stat().st_mtime_ns) == (content, mtime)


def public_client(monkeypatch, root):
    # Redirect storage only. web._load_articles/_load_article and both native
    # core readers remain real, including schema, rights and suffix validation.
    original_directory = core.archive_directory
    monkeypatch.setattr(
        core, "archive_directory",
        lambda directory_root=None, *, local=False: original_directory(
            root if directory_root is None else directory_root, local=local),
    )
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "bot/templates"))
    templates.env.globals["is_admin"] = lambda request: False
    application = FastAPI()
    application.include_router(web.make_analytics_articles_router(templates))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test")


class Markup(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.cards = self.charts = self.tables = 0
        self.ids, self.links, self.json_ld = [], [], []
        self.canonical = None
        self.capturing_json = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.cards += "data-analytics-card" in attrs
        self.charts += "data-analytics-chart" in attrs
        self.tables += tag == "table"
        if attrs.get("id"):
            self.ids.append(attrs["id"])
        if tag == "a":
            self.links.append(attrs.get("href"))
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs["href"]
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.capturing_json = True

    def handle_data(self, value):
        if self.capturing_json:
            self.json_ld.append(value)

    def handle_endtag(self, tag):
        if tag == "script":
            self.capturing_json = False


@pytest.mark.asyncio
async def test_real_archive_keeps_both_pairs_filters_and_unique_preview_ids(monkeypatch, tmp_path):
    base = publish_base(tmp_path)
    assert core.publish_edition(local_edition(), root=tmp_path, now=NOW, local=True)["status"] == "published"
    local = tmp_path / "var/analytics-local-articles" / f"{DAY}.json"
    snapshots = frozen(base, local)
    async with public_client(monkeypatch, tmp_path) as client:
        response = await client.get("/analytics")
        assert response.status_code == 200
        page = Markup(response.text)
        assert page.cards == 4
        assert len(page.ids) == len(set(page.ids))
        for kind in ("market", "rent"):
            for suffix in ("", "-local"):
                assert f"/analytics/{DAY}-{kind}{suffix}" in page.links
        filtered = await client.get("/analytics?category=rent")
        assert filtered.status_code == 200
        rentals = Markup(filtered.text)
        assert rentals.cards == 2
        assert f"/analytics/{DAY}-rent-local" in rentals.links
        assert f"/analytics/{DAY}-rent" in rentals.links
        assert f"/analytics/{DAY}-market-local" not in rentals.links
        assert "{fact:" not in response.text and "{label:" not in response.text
    assert_frozen(snapshots)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["market", "rent"])
async def test_real_local_detail_has_own_canonical_schema_tables_sources_and_original_route(monkeypatch, tmp_path, kind):
    base = publish_base(tmp_path)
    value = local_edition()
    core.publish_edition(value, root=tmp_path, now=NOW, local=True)
    local = tmp_path / "var/analytics-local-articles" / f"{DAY}.json"
    snapshots = frozen(base, local)
    path = f"/analytics/{DAY}-{kind}-local"
    async with public_client(monkeypatch, tmp_path) as client:
        response = await client.get(path + "?contact=PRIVATE-VISITOR")
        assert response.status_code == 200
        page = Markup(response.text)
        expected = core.load_article(f"{DAY}-{kind}-local", root=tmp_path, now=NOW)
        assert page.canonical == "https://hatuli.ai-groundtruth.com" + path
        schema = json.loads("".join(page.json_ld))
        assert schema["@type"] == "Article"
        assert schema["mainEntityOfPage"] == page.canonical
        assert schema["datePublished"] == DAY
        assert schema["headline"] == expected["title"]
        assert page.charts == page.tables == 2
        assert len(page.ids) == len(set(page.ids))
        assert response.text.count('<th scope="col">Наблюдений</th>') == 2
        assert "30 наблюдений" in response.text
        assert "Данные графика" in response.text
        assert expected["published_label"] in response.text
        assert all(source["url"] in page.links for source in expected["sources"])
        assert "/analytics" in page.links
        assert ("/best-rentals" if kind == "rent" else "/best-apartments") in page.links
        assert "PRIVATE-VISITOR" not in response.text
        assert "{fact:" not in response.text and "{label:" not in response.text
        if kind == "market":
            assert PLACE not in response.text
            assert "ЖК Geneva 4 &#34;Парк&#34;" in response.text
        original = await client.get(f"/analytics/{DAY}-{kind}")
        assert original.status_code == 200
        assert Markup(original.text).canonical == f"https://hatuli.ai-groundtruth.com/analytics/{DAY}-{kind}"
    assert_frozen(snapshots)


@pytest.mark.asyncio
async def test_missing_local_pair_never_falls_back_to_original_article(monkeypatch, tmp_path):
    base = publish_base(tmp_path)
    snapshots = frozen(base)
    async with public_client(monkeypatch, tmp_path) as client:
        assert (await client.get(f"/analytics/{DAY}-market-local")).status_code == 404
        assert Markup((await client.get("/analytics")).text).cards == 2
    assert_frozen(snapshots)


@pytest.mark.asyncio
async def test_unused_observed_source_cannot_create_public_local_routes(monkeypatch, tmp_path):
    base = publish_base(tmp_path)
    snapshots = frozen(base)
    value = edition()
    source = deepcopy(value["digest"]["sources"]["bns.housing"])
    source.update(kind="hatuli_observations", rights_status="operator_authorized",
                  url="https://hatuli.ai-groundtruth.com/how-it-works",
                  reuse_url="https://hatuli.ai-groundtruth.com/how-it-works")
    value["digest"]["sources"]["hatuli.unused"] = source
    with pytest.raises(ValueError, match="four used numeric"):
        core.publish_edition(value, root=tmp_path, now=NOW, local=True)
    async with public_client(monkeypatch, tmp_path) as client:
        assert (await client.get(f"/analytics/{DAY}-market-local")).status_code == 404
        assert (await client.get(f"/analytics/{DAY}-rent-local")).status_code == 404
        assert Markup((await client.get("/analytics")).text).cards == 2
    assert not (tmp_path / "var/analytics-local-articles" / f"{DAY}.json").exists()
    assert_frozen(snapshots)


@pytest.mark.parametrize("place", ["ЖК {fact:sales.rooms_2}", "Скидка 90% и доходность 50%", "ЖК Выгода 20 процентов"])
def test_invalid_bound_label_cannot_write_a_public_supplement(tmp_path, place):
    base = publish_base(tmp_path)
    snapshots = frozen(base)
    value = local_edition()
    value["digest"]["facts"]["sales.rooms_1"]["label"] = place
    with pytest.raises(ValueError, match="label token requires"):
        core.publish_edition(value, root=tmp_path, now=NOW, local=True)
    assert core.load_article(f"{DAY}-market-local", root=tmp_path, now=NOW) is None
    assert len(core.list_articles(root=tmp_path, now=NOW)) == 2
    assert_frozen(snapshots)


def test_local_charts_cannot_borrow_official_fact_despite_four_observed_prose_facts(tmp_path):
    base = publish_base(tmp_path)
    snapshots = frozen(base)
    value = local_edition()
    value["digest"]["sources"]["bns.official"] = digest()["sources"]["bns.housing"]
    value["digest"]["facts"]["sales.rooms_4"]["source_id"] = "bns.official"
    value["articles"][0]["source_ids"].append("bns.official")
    value["digest"]["facts"]["sales.extra"] = deepcopy(value["digest"]["facts"]["sales.rooms_2"])
    value["articles"][0]["lead"] += " Дополнительный ориентир этого профиля составляет {fact:sales.extra}."
    with pytest.raises(ValueError, match="charts require observed"):
        core.publish_edition(value, root=tmp_path, now=NOW, local=True)
    assert len(core.list_articles(root=tmp_path, now=NOW)) == 2
    assert_frozen(snapshots)


@pytest.mark.parametrize("kind,prefix", [("market", "sales"), ("rent", "rent_monthly")])
def test_each_local_article_requires_four_numeric_observations_not_place_labels(tmp_path, kind, prefix):
    base = publish_base(tmp_path)
    snapshots = frozen(base)
    value = local_edition()
    value["digest"]["sources"]["bns.official"] = digest()["sources"]["bns.housing"]
    value["digest"]["facts"][f"{prefix}.rooms_4"]["source_id"] = "bns.official"
    extra = {**value["digest"]["facts"][f"{prefix}.rooms_1"], "label": "ЖК Geneva 4"}
    value["digest"]["facts"][f"{prefix}.extra_place"] = extra
    article = next(item for item in value["articles"] if item["kind"] == kind)
    article["source_ids"].append("bns.official")
    article["lead"] += f" Отдельная метка места — {{label:{prefix}.extra_place}}."
    with pytest.raises(ValueError, match="four used numeric"):
        core.publish_edition(value, root=tmp_path, now=NOW, local=True)
    assert len(core.list_articles(root=tmp_path, now=NOW)) == 2
    assert_frozen(snapshots)
