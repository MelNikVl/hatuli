"""The real application exposes the file archive and its discoverable URLs."""
import httpx
import pytest

from bot.admin_web import create_admin_app


@pytest.mark.asyncio
async def test_articles_registered_and_discoverable_without_listing_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/hatuli_test")

    async def reject(*args, **kwargs):
        raise AssertionError("Article integration tests must not connect to PostgreSQL")

    async def no_complex_rows(*args, **kwargs):
        return []

    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    monkeypatch.setattr("bot.db.pg.fetch", no_complex_rows)
    # Sitemap reads the canonical, validated archive; routes have no DB reads.
    monkeypatch.setattr("bot.core.analytics_articles.list_articles", lambda **kwargs: [{
        "url": "/analytics/2026-10-16-market", "edition_date": "2026-10-16",
    }] if not kwargs else [])
    monkeypatch.setattr("bot.core.analytics_articles.load_article", lambda slug: None)
    app = create_admin_app(None, "isolated-article-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        archive = await client.get("/analytics")
        assert archive.status_code == 200
        assert 'href="/analytics" aria-current="page"' in archive.text
        assert 'href="/admin-panel"' not in archive.text
        assert (await client.get("/analytics/2026-10-16-rent")).status_code == 404
        sitemap = await client.get("/sitemap.xml")
        assert sitemap.status_code == 200
        assert "https://hatuli.ai-groundtruth.com/analytics</loc>" in sitemap.text
        assert "/analytics/2026-10-16-market</loc><lastmod>2026-10-16</lastmod>" in sitemap.text
        assert "https://hatuli.ai-groundtruth.com/analytics" in (await client.get("/llms.txt")).text
