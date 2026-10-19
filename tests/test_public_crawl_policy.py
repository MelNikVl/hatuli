"""Offline crawl policy and actual application sitemap responses."""
from datetime import datetime, timedelta, timezone
import hashlib
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import httpx
import pytest

from bot.admin_web import create_admin_app
from bot.core import analytics_articles as articles
from bot.core.sitemap import (COMPLEX_SITEMAP_SQL, CRAWLER_USER_AGENTS, PUBLIC_ORIGIN,
                              PUBLIC_RENDER_PATHS, article_sitemap_entry)
from tests.test_analytics_articles import NOW, brief_digest, brief_draft, edition


@pytest.fixture(autouse=True)
def deny_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/hatuli_crawl_test")

    async def reject(*args, **kwargs):
        raise AssertionError("Crawl tests must not initialize or connect to PostgreSQL")

    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    monkeypatch.setattr("bot.db.pg.init_pool", reject)
    monkeypatch.setattr("bot.db.pg.fetch", reject)


def client():
    app = create_admin_app(None, "isolated-crawl-form-secret", "test")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


def crawl_allowed(document, user_agent, url):
    """REP group selection and longest match, including '*' and '$' patterns.

    urllib.robotparser uses first-rule order, which cannot test the actual
    Google/RFC longest-path behavior for a public API exception under /admin.
    """
    groups = []
    agents, rules = [], []
    for raw in document.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        name, value = (part.strip() for part in line.split(":", 1))
        name = name.lower()
        if name == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif name in ("allow", "disallow"):
            rules.append((name, value))
    if agents:
        groups.append((agents, rules))
    agent = user_agent.lower()
    specific = [(names, values) for names, values in groups if any(name != "*" and name in agent for name in names)]
    applicable = specific or [(names, values) for names, values in groups if "*" in names]
    parsed = urlsplit(url)
    target = parsed.path + ("?" + parsed.query if parsed.query else "")
    matches = []
    for _, values in applicable:
        for name, value in values:
            if not value:
                continue
            anchored = value.endswith("$")
            pattern = value[:-1] if anchored else value
            regex = "^" + re.escape(pattern).replace(r"\*", ".*") + ("$" if anchored else "")
            if re.search(regex, target):
                matches.append((len(value.replace("*", "").rstrip("$")), name == "allow"))
    return max(matches, default=(0, True))[1]


@pytest.mark.asyncio
async def test_robots_keeps_every_existing_crawler_public_but_private_exclusions_apply_to_all():
    async with client() as browser:
        response = await browser.get("/robots.txt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    document = response.text
    assert [line.split(":", 1)[1].strip() for line in document.splitlines() if line.startswith("User-agent:")] == list(CRAWLER_USER_AGENTS)
    assert document.count("Sitemap:") == 1
    assert f"Sitemap: {PUBLIC_ORIGIN}/sitemap.xml" in document
    assert "Content-Signal" not in document
    for agent in (*CRAWLER_USER_AGENTS[1:], "Googlebot", "Googlebot-Image", "Bingbot", "UnknownCrawler"):
        for path in ("/", "/analytics", "/analytics/2026-10-16-market", "/analytics/2026-10-16-rent-local",
                     "/analytics?category=rent&topic=banks", "/compare", "/best-apartments", "/best-rentals",
                     "/map", "/complexes", "/complex/123", "/developers", "/static/public.css",
                     "/static/branding/hatuli-share.png", "/sitemap.xml"):
            assert crawl_allowed(document, agent, PUBLIC_ORIGIN + path), (agent, path)
        for path in ("/admin/login", "/admin/settings", "/admin-panel", "/admin/api/rental-health",
                     "/api/auth/poll?token=PRIVATE", "/api/favorites/ids", "/api/listing/123/rental-economics",
                     "/auth/telegram", "/cabinet", "/favorites", "/request-analysis",
                     "/buyer/map/api/session?nonce=PRIVATE", "/rental/daily", "/rental/scanner"):
            assert not crawl_allowed(document, agent, PUBLIC_ORIGIN + path), (agent, path)


@pytest.mark.asyncio
async def test_only_exact_public_map_render_resources_override_admin_crawl_exclusion():
    async with client() as browser:
        document = (await browser.get("/robots.txt")).text
    for path in PUBLIC_RENDER_PATHS:
        for suffix in ("", "?year=2026", "?rooms=2&developers=1"):
            assert crawl_allowed(document, "Googlebot", PUBLIC_ORIGIN + path + suffix)
        for suffix in ("-private", "/delete", "/sessions"):
            assert not crawl_allowed(document, "Googlebot", PUBLIC_ORIGIN + path + suffix)
    for path in ("/admin/api/map-points?type=rental", "/admin/api/listing/123", "/admin/api/complex/create"):
        assert not crawl_allowed(document, "Googlebot", PUBLIC_ORIGIN + path)


@pytest.mark.parametrize("url", ["/admin/login", "https://evil.test/analytics/2026-10-16-market",
                                  "/analytics/2026-10-16-market?token=PRIVATE", "/analytics/2026-10-16-rent#contact",
                                  "/analytics/2026-02-30-rent", "/analytics/2026-10-16-market<script>"])
def test_sitemap_helper_does_not_expand_article_urls_to_arbitrary_or_visitor_paths(url):
    assert article_sitemap_entry({"url": url, "edition_date": "2026-10-16"}) is None


@pytest.mark.parametrize("modified,published,expected", [
    ("2026-10-19T18:15:00+05:00", "2026-10-16T06:00:00Z", "2026-10-19T18:15:00+05:00"),
    (None, "2026-10-16T06:00:00Z", "2026-10-16T06:00:00+00:00"),
    ("2026-10-19T18:15:00", "2026-10-16T06:00:00Z", "2026-10-16T06:00:00+00:00"),
    ("bad", None, "2026-10-16"),
])
def test_article_lastmod_uses_real_revision_or_publication_never_request_time(modified, published, expected):
    assert article_sitemap_entry({"url": "/analytics/2026-10-16-market", "edition_date": "2026-10-16",
                                  "date_modified": modified, "published_at": published}) == (
                                      PUBLIC_ORIGIN + "/analytics/2026-10-16-market", expected)


@pytest.mark.asyncio
async def test_real_immutable_article_revision_changes_sitemap_lastmod_without_retiming_base(monkeypatch, tmp_path):
    articles.publish_edition(edition(), root=tmp_path, now=NOW)
    original = articles.archive_directory(tmp_path) / "2026-10-16.json"
    frozen = original.read_bytes(), original.stat().st_mtime_ns
    modified = datetime(2026, 10, 19, 18, 0, tzinfo=timezone(timedelta(hours=5)))
    revision = articles.build_revision(brief_draft(), brief_digest(modified), root=tmp_path,
                                        base_sha256=hashlib.sha256(frozen[0]).hexdigest(),
                                        reason="Уточнены практические выводы и графики без изменения исходной даты", now=modified)
    articles.publish_revision(revision, root=tmp_path, now=modified)
    native_list = articles.list_articles
    monkeypatch.setattr(articles, "list_articles", lambda: native_list(root=tmp_path, now=modified))
    calls = []

    async def eligible_complexes(query):
        assert query == COMPLEX_SITEMAP_SQL
        calls.append(query)
        return [{"id": 123, "updated_at": datetime(2026, 10, 18, tzinfo=timezone.utc), "active_cnt": 2}]

    monkeypatch.setattr("bot.db.pg.fetch", eligible_complexes)
    async with client() as browser:
        first = await browser.get("/sitemap.xml")
        second = await browser.get("/sitemap.xml")
    assert first.status_code == second.status_code == 200
    assert first.headers["content-type"].startswith("application/xml")
    assert first.text == second.text
    root = ET.fromstring(first.text)
    namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = {entry.findtext("s:loc", namespaces=namespace): entry.findtext("s:lastmod", namespaces=namespace)
            for entry in root.findall("s:url", namespace)}
    for kind in ("market", "rent"):
        assert urls[f"{PUBLIC_ORIGIN}/analytics/2026-10-16-{kind}"] == modified.astimezone(timezone.utc).isoformat()
        current = articles.load_article(f"2026-10-16-{kind}", root=tmp_path, now=modified)
        assert current["published_at"] == NOW.isoformat()
        assert current["date_modified"] == modified.astimezone(timezone.utc).isoformat()
    assert urls[f"{PUBLIC_ORIGIN}/complex/123"] == "2026-10-18"
    assert not any("admin" in url or "api/" in url or "PRIVATE" in url for url in urls)
    assert len(calls) == 2
    assert (original.read_bytes(), original.stat().st_mtime_ns) == frozen


@pytest.mark.asyncio
async def test_robots_does_not_replace_private_route_authentication():
    async with client() as browser:
        response = await browser.get("/admin/settings", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/admin/login"
        api = await browser.get("/admin/api/rental-health")
        assert api.status_code == 403
        assert api.headers["cache-control"] == "no-store"
